"""Nullable, bounded Run metadata; refuse lossy downgrade."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '20261104_0010'
down_revision = '20261030_0009'
branch_labels = None
depends_on = None

FIELDS = [('request_metadata',2048),('execution_metadata',2048),('result_metadata',16384)]


def upgrade():
    for name,limit in FIELDS:
        op.add_column('agent_runs',sa.Column(name,postgresql.JSONB(none_as_null=True),nullable=True))
        op.create_check_constraint(f'agent_run_{name}_size','agent_runs',
            f"{name} IS NULL OR (jsonb_typeof({name}) = 'object' AND octet_length({name}::text) <= {limit})")


def downgrade():
    connection=op.get_bind()
    connection.execute(sa.text('LOCK TABLE agent_runs IN ACCESS EXCLUSIVE MODE'))
    if connection.execute(sa.text('SELECT EXISTS (SELECT 1 FROM agent_runs WHERE '+
                                 ' OR '.join(name+' IS NOT NULL' for name,_ in FIELDS)+')')).scalar_one():
        raise RuntimeError('Refusing to remove Run metadata. Back up and explicitly migrate metadata before downgrade.')
    for name,_ in reversed(FIELDS):
        op.drop_constraint(op.f(f'ck_agent_runs_agent_run_{name}_size'),'agent_runs',type_='check')
        op.drop_column('agent_runs',name)
