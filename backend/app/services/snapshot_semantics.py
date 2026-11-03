"""Snapshot-specific orchestration on the existing task/Artifact infrastructure."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from uuid import UUID

from backend.app.config import EmbeddingSettings, RetrievalSettings
from backend.app.core.errors import BackendError
from backend.app.integrations.embedding import TencentEmbeddingProvider, MODEL_DIMENSIONS
from backend.app.integrations.semantic_index import (
    SemanticIndexBuilder, EmbeddingError, identity, SnapshotBinding, SemanticIdentity, file_hash,
    VectorUnavailable, validate_result,
)

SEMANTIC_TASK_TYPE = 'snapshot_embedding'
logger = logging.getLogger('pycode.backend.tasks')


class SnapshotSemantics:
    def __init__(self, uow, artifacts, *, settings_factory=EmbeddingSettings.from_environment,
                 provider_factory=TencentEmbeddingProvider, dispatcher=None, admission=None,
                 retrieval_settings_factory=RetrievalSettings.from_environment):
        self.uow, self.artifacts = uow, artifacts
        self.settings_factory, self.provider_factory = settings_factory, provider_factory
        self.dispatcher, self.admission = dispatcher, admission
        self.retrieval_settings_factory = retrieval_settings_factory

    def retrieval(self, context, snapshot):
        """Bind a lazy query capability, after Service authorization/checkout.

        It captures configuration for this execution; Artifact IO and Provider
        creation occur only if the unified Retriever actually selects Vector.
        """
        try:
            config = self.retrieval_settings_factory()
        except (ValueError, TypeError, RuntimeError):
            def invalid(*args):
                raise VectorUnavailable("vector_configuration")
            return replace(context, retrieval_mode="vector", vector_query=invalid)
        if config.mode == "keyword":
            return context
        try:
            settings = self._settings()
        except BackendError:
            settings = None

        def query(text, limit):
            if settings is None:
                raise VectorUnavailable("vector_configuration")
            if not settings.enabled:
                raise VectorUnavailable("vector_disabled")
            if not isinstance(text, str) or not text.strip() or len(text) > settings.max_input_chars:
                raise VectorUnavailable("vector_query_too_long")
            if context.binding != SnapshotBinding(str(snapshot.project_id), str(snapshot.id), snapshot.commit_sha):
                raise VectorUnavailable("vector_binding_mismatch")
            try:
                target, chunks = self.target(snapshot, settings)
                if chunks != context.chunks or target.binding != context.binding:
                    raise VectorUnavailable("vector_binding_mismatch")
                index = self.artifacts.read_vector_index(target, chunks)
            except BackendError:
                raise VectorUnavailable("vector_artifact_unavailable") from None
            # This check also catches accidental SQLAlchemy autobegin outside
            # an explicit UoW block; no rollback/commit is performed to hide it.
            session = getattr(self.uow, "session", None)
            if session is not None and session.in_transaction():
                raise VectorUnavailable("vector_unavailable")
            provider = None
            try:
                provider = self.provider_factory(settings)
                if (provider.provider, provider.model, provider.dimension) != (target.provider, target.model, target.dimension):
                    raise VectorUnavailable("vector_configuration")
                response = provider.embed_query(text)
                validate_result(response, 1, target.provider, target.model, target.dimension)
                return index.search(response, limit)
            except VectorUnavailable:
                raise
            except EmbeddingError as exc:
                code = "vector_invalid_response" if exc.code == "embedding_invalid_response" else "vector_provider_failed"
                raise VectorUnavailable(code) from None
            except (ValueError, TypeError, AttributeError, OverflowError):
                raise VectorUnavailable("vector_invalid_response") from None
            except Exception:
                raise VectorUnavailable("vector_provider_failed") from None
            finally:
                if provider is not None and callable(close := getattr(provider, "close", None)):
                    try:
                        close()
                    except Exception:
                        pass  # No SDK exception or secret-bearing message is logged.

        return replace(context, retrieval_mode="vector", vector_top_k=config.vector_top_k, vector_query=query)

    def _settings(self):
        try:
            return self.settings_factory()
        except (ValueError, TypeError, RuntimeError):
            raise BackendError('embedding_configuration', 'Embedding configuration is invalid.') from None

    def target(self, snapshot, settings):
        chunks = self.artifacts.read_chunks(snapshot.project_id, snapshot)
        if chunks is None:
            raise BackendError('artifacts_unavailable', 'This Snapshot has no Chunk Artifact. Build its base chunks first.')
        bundle = self.artifacts.read_ready(snapshot.project_id, snapshot)
        target = SemanticIdentity(chunks.binding, file_hash(bundle.chunks_path), settings.provider,
                                  settings.model, MODEL_DIMENSIONS[settings.model],
                                  settings.max_input_chars, settings.base_url.rstrip('/'), chunks.schema_version)
        return target, chunks

    def automatic_intent(self, snapshot, bundle=None):
        """Prepare outside the activation transaction; invalid config cannot fail base indexing."""
        try:
            settings = self._settings()
        except BackendError:
            return identity({'snapshot': str(snapshot.id), 'configuration': 'invalid'})
        if not settings.enabled:
            return None
        if bundle is None:
            bundle = self.artifacts.read_ready(snapshot.project_id, snapshot)
        if bundle.chunks_path is None:
            return identity({'snapshot': str(snapshot.id), 'chunks': 'missing'})
        return SemanticIdentity(
            SnapshotBinding(str(snapshot.project_id), str(snapshot.id), snapshot.commit_sha),
            file_hash(bundle.chunks_path), settings.provider, settings.model,
            MODEL_DIMENSIONS[settings.model], settings.max_input_chars,
            settings.base_url.rstrip('/'),
        ).fingerprint

    def completion_for(self, task, snapshot_id=None):
        """Prepare file metadata outside the terminal-write transaction, including recovery."""
        from backend.app.services.background_tasks import REPOSITORY_INDEX_TASK_TYPE, TaskCompletion
        snapshot_id = snapshot_id or task.snapshot_id
        if snapshot_id is None:
            return None
        with self.uow.transaction():
            current = self.uow.background_tasks.get(task.id)
            if (current.task_type != REPOSITORY_INDEX_TASK_TYPE or current.project_id != task.project_id
                    or current.snapshot_id != snapshot_id or current.execution_token != task.execution_token
                    or current.status != 'running'):
                raise BackendError('background_task_consistency_error', 'Index continuation binding is invalid.')
            self.uow.projects.get(task.project_id)
            snapshot = self.uow.snapshots.get_for_project(task.project_id, snapshot_id)
        if snapshot.status != 'ready':
            return None
        fingerprint = self.automatic_intent(snapshot)
        if fingerprint is None:
            return None
        return TaskCompletion(snapshot.id,
                              lambda: self.uow.projects.get(snapshot.project_id, for_update=True),
                              lambda: self.reserve_automatic(snapshot, fingerprint, task.id), self.enqueue)

    def reserve_automatic(self, snapshot, fingerprint, source_task_id):
        """Caller completes the parent first, then transfers its slot in the same transaction."""
        from backend.app.services.background_tasks import REPOSITORY_INDEX_TASK_TYPE
        if fingerprint is None:
            return None
        parent = self.uow.background_tasks.get(source_task_id)
        if (parent.task_type != REPOSITORY_INDEX_TASK_TYPE or parent.status != 'completed'
                or parent.project_id != snapshot.project_id or parent.snapshot_id != snapshot.id):
            raise BackendError('background_task_consistency_error', 'Index continuation parent is invalid.')
        creation = self.uow.background_tasks.create_submission(
            SEMANTIC_TASK_TYPE, project_id=snapshot.project_id, snapshot_id=snapshot.id,
            idempotency_key='auto:' + str(source_task_id) + ':' + fingerprint,
            request_fingerprint=fingerprint,
        )
        task = creation.task
        project = self.uow.projects.get(snapshot.project_id)
        if creation.created and self.admission is not None and project.owner_id is not None:
            try:
                self.admission.admit_task(self.uow, project.owner_id, 'index', count_request=False)
            except BackendError as exc:
                if exc.code != 'task_quota_exceeded':
                    raise
                claimed = self.uow.background_tasks.claim(task.id).task
                task = self.uow.background_tasks.mark_failed(
                    task.id, execution_token=claimed.execution_token,
                    error_code='task_quota_exceeded',
                    error_message='Semantic task quota exceeded; base Snapshot remains available.')
        return task

    def enqueue(self, task):
        if task is not None and task.status == 'queued' and self.dispatcher is not None:
            from backend.app.services.background_tasks import BackgroundTaskService
            try:
                BackgroundTaskService(self.uow, self.dispatcher).enqueue_semantic(task)
            except BackendError:
                # The durable task is already committed with the ready base bundle.
                logger.warning('Semantic task awaiting queued reconciliation task_id=%s', task.id)

    def build_task(self, task, ownership, operations):
        if task.project_id is None or task.snapshot_id is None:
            raise BackendError('background_task_consistency_error', 'Semantic task binding is invalid.')
        with operations.operation(task.project_id) as lock:
            with self.uow.transaction():
                self.uow.projects.get(task.project_id)
                snapshot = self.uow.snapshots.get_for_project(task.project_id, task.snapshot_id)
            settings = self._settings()
            if not settings.enabled:
                raise BackendError('embedding_unavailable', 'Embedding is disabled; base Snapshot remains available.')
            target, chunks = self.target(snapshot, settings)
            if task.request_fingerprint != target.fingerprint:
                raise BackendError('embedding_configuration', 'Embedding target changed; submit a new task. Base Snapshot remains available.')

            def guard():
                lock.ensure_owned()
                ownership.ensure_owned()
                with self.uow.transaction():
                    self.uow.projects.get(task.project_id)
                    current = self.uow.snapshots.get_for_project(task.project_id, task.snapshot_id)
                if current.status != 'ready' or current.commit_sha != snapshot.commit_sha:
                    raise BackendError('artifacts_unavailable', 'Semantic Snapshot binding changed.')

            guard()
            # Do not instantiate a Provider or call it when a valid version exists.
            if self.artifacts.recover_semantic(target, chunks, before_publish=guard) is not None:
                return snapshot.id
            provider = None
            try:
                provider = self.provider_factory(settings)
                SemanticIndexBuilder(self.artifacts).build(
                    target, chunks, provider, batch_size=settings.batch_size, guard=guard)
            except EmbeddingError as exc:
                raise BackendError(exc.code, 'Semantic indexing failed; base Snapshot remains available.') from None
            except (ValueError, TypeError, OSError):
                raise BackendError('embedding_failed', 'Semantic artifact construction failed; base Snapshot remains available.') from None
            finally:
                if provider is not None and callable(getattr(provider, 'close', None)):
                    provider.close()
            return snapshot.id

    def task_ready(self, task):
        try:
            with self.uow.transaction():
                snapshot = self.uow.snapshots.get_for_project(task.project_id, task.snapshot_id)
            settings = self._settings()
            target, chunks = self.target(snapshot, settings)
            return task.request_fingerprint == target.fingerprint and self.artifacts.read_semantic(target, chunks) is not None
        except (BackendError, ValueError, OSError):
            return False

    def describe(self, snapshot):
        result = {'status': 'not_built', 'provider': None, 'model': None, 'dimension': None,
                  'fingerprint': None, 'artifact': None, 'built_at': None,
                  'error_code': None, 'error_message': None, 'versions': []}
        try:
            settings = self._settings()
        except BackendError:
            result.update(status='failed', error_code='embedding_configuration',
                          error_message='Embedding configuration is invalid.')
            return result
        if snapshot.status != 'ready':
            result['status'] = 'not_built' if settings.enabled else 'disabled'
            return result
        try:
            # Even disabled configuration may expose already published versions.
            bundle = self.artifacts.read_ready(snapshot.project_id, snapshot)
            if bundle.chunks_path is None:
                result['status'] = 'not_built' if settings.enabled else 'disabled'
                return result
            target, chunks = self.target(snapshot, settings)
            result.update(provider=target.provider, model=target.model, fingerprint=target.fingerprint)
            parent = self.artifacts.semantic_directory(target).parent
            if parent.exists():
                root = self.artifacts._project_root(snapshot.project_id)
                self.artifacts._check_entry(parent, root)
                for directory in sorted(parent.iterdir()):
                    if len(directory.name) != 64 or any(c not in '0123456789abcdef' for c in directory.name):
                        continue
                    self.artifacts._check_entry(directory, root)
                    ready = directory / 'ready'
                    if not ready.exists():
                        continue
                    self.artifacts._check_tree(ready, root)
                    try:
                        metadata = json.loads((ready / 'manifest.json').read_text(encoding='utf-8'))
                        fields = dict(metadata['identity'])
                        fields.pop('binding')
                        version = SemanticIdentity(chunks.binding, **fields)
                        if version.chunk_hash != target.chunk_hash or directory.name != version.fingerprint:
                            continue
                        manifest = self.artifacts.read_semantic(version, chunks)
                    except (BackendError, OSError, ValueError, KeyError, TypeError, AttributeError):
                        continue
                    result['versions'].append(self._summary(manifest))
            with self.uow.transaction():
                tasks = self.uow.background_tasks.list_for_snapshot(snapshot.project_id, snapshot.id, SEMANTIC_TASK_TYPE)
            task = next((t for t in tasks if t.request_fingerprint == target.fingerprint), None)
            current = next((v for v in result['versions'] if v['fingerprint'] == target.fingerprint), None)
            if current:
                result.update(current, status='ready')
            elif task:
                result['status'] = {'running': 'building', 'queued': 'queued',
                                    'failed': 'failed', 'completed': 'unavailable'}[task.status]
                result['error_code'] = task.error_code
                result['error_message'] = task.error_message
            if not settings.enabled:
                result['status'] = 'disabled'
        except (BackendError, OSError, ValueError, TypeError, KeyError):
            result.update(status='disabled' if not settings.enabled else 'unavailable',
                          error_code='artifacts_unavailable', error_message='Semantic artifacts are unavailable.')
        return result

    @staticmethod
    def _summary(manifest):
        fields = manifest['identity']
        return {'provider': fields['provider'], 'model': fields['model'],
                'dimension': fields['dimension'], 'fingerprint': manifest['fingerprint'],
                'built_at': manifest['built_at'], 'artifact': {
                    'format_version': fields['format_version'], 'dtype': manifest['dtype'],
                    'dimension_source': manifest['dimension_source'],
                    'byte_order': manifest['byte_order'], 'vector_count': manifest['vector_count'],
                    'byte_count': manifest['byte_count'], 'sha256': manifest['vectors_sha256']}}
