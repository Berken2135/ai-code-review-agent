"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_pull_requests"),
        sa.UniqueConstraint("repo_full_name", "number", name="uq_pull_requests_repo_full_name"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("trigger_delivery_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("review_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_runs_status"
        ),
        sa.ForeignKeyConstraint(
            ["pr_id"], ["pull_requests.id"], name="fk_runs_pr_id_pull_requests"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.UniqueConstraint("trigger_delivery_id", name="uq_runs_trigger_delivery_id"),
    )
    op.create_index("ix_runs_pr_id", "runs", ["pr_id"])
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("suggested_test_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "category IN ('bug', 'security', 'performance', 'quality', 'edge_case', "
            "'missing_test')",
            name="ck_findings_category",
        ),
        sa.CheckConstraint("severity IN ('high', 'medium', 'low')", name="ck_findings_severity"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_findings_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_findings"),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"])
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("node", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_llm_calls_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
    )
    op.create_index("ix_llm_calls_run_id", "llm_calls", ["run_id"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("findings")
    op.drop_table("runs")
    op.drop_table("pull_requests")
