from sqlalchemy import text

from b09_query_benchmark import (
    PROJECTS, RUNS, REPEATS, PROJECT_INSERT, PROJECT_UPDATE, RUN_INSERT, RUN_COMPLETE,
    environment, history_cases, indexes, measure, save_report, seed, write_controls,
)
from test_postgresql import _safe_test_database_url, _truncate, postgresql_engine


def test_history_pagination_plans_results_and_write_controls(postgresql_engine):
    """Run directly with pytest -s for the reproducible B09 benchmark."""
    _safe_test_database_url()
    engine = postgresql_engine
    _truncate(engine)
    report = {"projects": PROJECTS, "agent_runs": RUNS, "owners": 50,
              "history_projects": 20, "warmups": 2, "samples": REPEATS,
              "write_batch_rows": 500, "queries": {}, "write_controls": {}}
    try:
        with engine.begin() as connection:
            seed(connection)
        with engine.begin() as connection:
            report["environment"] = environment(connection)
            before_indexes = indexes(connection)
            report["indexes"] = before_indexes
            report["target_distribution"] = [dict(row) for row in connection.execute(text("""
                SELECT status,run_type,result IS NOT NULL AS has_result,count(*) AS rows
                FROM agent_runs WHERE project_id=md5('project-1')::uuid
                GROUP BY status,run_type,result IS NOT NULL ORDER BY status,run_type,has_result
            """)).mappings()]
            report["table_bytes"] = {table: connection.execute(text(
                "SELECT pg_total_relation_size(:table)"
            ), {"table": table}).scalar_one() for table in ["projects", "agent_runs"]}
            for name, sql in history_cases(connection).items():
                if name.endswith("_offset_0"):
                    assert sql["before"] == sql["after"]
                before_rows = list(connection.execute(text(sql["before"])))
                after_rows = list(connection.execute(text(sql["after"])))
                assert before_rows == after_rows
                before, after = measure(connection, sql["before"]), measure(connection, sql["after"])
                report["queries"][name] = {"before": before, "after": after,
                                           "rows": len(after_rows), "ids": [str(row.id) for row in after_rows]}
                print(name, before["median_execution_ms"], after["median_execution_ms"])
            for name, sql in [("project_insert", PROJECT_INSERT), ("project_update", PROJECT_UPDATE),
                              ("run_insert", RUN_INSERT), ("run_complete", RUN_COMPLETE)]:
                values = write_controls(connection, sql)
                report["write_controls"][name] = values
                print(name, values["before"]["median_execution_ms"], values["after"]["median_execution_ms"])
            assert indexes(connection) == before_indexes
        print(f"B09 report: {save_report('history_benchmark', report)}")
    finally:
        _truncate(engine)
