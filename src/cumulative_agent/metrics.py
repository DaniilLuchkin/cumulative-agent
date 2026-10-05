from .repository import Repository


class Metrics:
    def __init__(self, repo: Repository):
        self.repo = repo

    def snapshot(self) -> dict:
        totals = self.repo.one("SELECT COUNT(*) runs,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(cost),0) cost,AVG(latency) average_latency FROM usage")
        counts = {r["status"]: r["n"] for r in self.repo.all("SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
        completed = counts.get("done", 0)
        return {**totals, "tasks_completed": completed, "tasks_failed": counts.get("failed", 0),
                "background_tasks_completed": self.repo.one("SELECT COUNT(*) n FROM tasks WHERE status='done' AND type!='user'")["n"],
                "resources_discovered": self.repo.one("SELECT COUNT(*) n FROM discoveries")["n"],
                "models_used": self.repo.all("SELECT model,COUNT(*) runs,SUM(cost) cost,AVG(success) success_rate,AVG(quality_score) quality_score FROM usage WHERE model IS NOT NULL GROUP BY model"),
                "cost_per_completed_task": totals["cost"] / completed if completed else None, "queue": counts,
                "usage_unknown_runs": self.repo.one("SELECT COUNT(*) n FROM usage WHERE usage_known=0")["n"],
                'background_yields':self.repo.one("SELECT COUNT(*) n FROM task_runs WHERE status='yielded'")['n'],
                'patches_created':self.repo.one("SELECT COUNT(*) n FROM artifacts WHERE kind='patch'")['n'],
                'patch_reviews_pending':self.repo.one("SELECT COUNT(*) n FROM approvals WHERE action='merge' AND status='pending'")['n'],
                'test_evaluations':self.repo.one("SELECT COUNT(*) n FROM run_evaluations WHERE source='tests'")['n'],
                "reserved_cost": self.repo.one("SELECT COALESCE(SUM(reserved_cost),0) n FROM task_runs WHERE status='running'")["n"]}
