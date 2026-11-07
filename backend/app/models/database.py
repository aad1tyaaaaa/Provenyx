from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, delete, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ScanRow(Base):
    __tablename__ = "scans"

    scan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    project_name: Mapped[str | None] = mapped_column(String(214))
    ecosystem: Mapped[str] = mapped_column(String(32), nullable=False, default="npm")
    package_manager: Mapped[str] = mapped_column(String(32), nullable=False, default="npm")
    component_path: Mapped[str] = mapped_column(String(512), nullable=False, default=".")
    lockfile: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    graph: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    simplified: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class ScanFindingRow(Base):
    __tablename__ = "scan_findings"

    finding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.scan_id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


def normalize_database_url(url: str) -> str:
    """Hosted Postgres providers (Neon, Render, Supabase) hand out postgres:// or postgresql:// URLs;
    SQLAlchemy needs the driver named to use psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class ScanRepository:
    def __init__(self, database_url: str) -> None:
        database_url = normalize_database_url(database_url)
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        # pre_ping: serverless Postgres closes idle connections; reconnect instead of failing a request.
        self._engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine)
        existing_columns = {column["name"] for column in inspect(self._engine).get_columns("scans")}
        if "owner_id" not in existing_columns:
            with self._engine.begin() as connection:
                connection.execute(text("ALTER TABLE scans ADD COLUMN owner_id VARCHAR(64)"))
        if "simplified" not in existing_columns:
            with self._engine.begin() as connection:
                connection.execute(text("ALTER TABLE scans ADD COLUMN simplified JSON"))
        identity_columns = {
            "ecosystem": ("VARCHAR(32)", "npm"),
            "package_manager": ("VARCHAR(32)", "npm"),
            "component_path": ("VARCHAR(512)", "."),
        }
        for column, (column_type, default) in identity_columns.items():
            if column not in existing_columns:
                with self._engine.begin() as connection:
                    connection.execute(
                        text(f"ALTER TABLE scans ADD COLUMN {column} {column_type} DEFAULT '{default}' NOT NULL")
                    )
        for index in ScanRow.__table__.indexes:
            index.create(self._engine, checkfirst=True)

    def create(
        self,
        scan_id: str,
        project_name: str | None,
        lockfile: dict[str, Any],
        owner_id: str | None = None,
        *,
        ecosystem: str = "npm",
        package_manager: str = "npm",
        component_path: str = ".",
    ) -> None:
        with self._sessions.begin() as session:
            session.add(
                ScanRow(
                    scan_id=scan_id,
                    owner_id=owner_id,
                    status="accepted",
                    created_at=datetime.now(timezone.utc),
                    project_name=project_name,
                    ecosystem=ecosystem,
                    package_manager=package_manager,
                    component_path=component_path,
                    lockfile=lockfile,
                    warnings=[],
                )
            )

    def set_analyzing(self, scan_id: str) -> None:
        self._update(scan_id, status="analyzing")

    def set_complete(
        self,
        scan_id: str,
        *,
        project_name: str | None,
        summary: dict[str, Any],
        graph: dict[str, Any],
        findings: list[dict[str, Any]],
        warnings: list[str],
        simplified: dict[str, Any] | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(ScanRow, scan_id)
            if row is None:
                raise KeyError(f"Unknown scan {scan_id}")
            row.status = "complete"
            row.project_name = project_name
            row.summary = summary
            row.graph = graph
            row.warnings = warnings
            row.simplified = simplified
            row.error = None
            session.execute(delete(ScanFindingRow).where(ScanFindingRow.scan_id == scan_id))
            session.add_all(
                ScanFindingRow(finding_id=finding["id"], scan_id=scan_id, payload=finding)
                for finding in findings
            )

    def set_failed(self, scan_id: str, error: str) -> None:
        self._update(scan_id, status="failed", error=error)

    def get(self, scan_id: str) -> ScanRow | None:
        with self._sessions() as session:
            return session.scalar(select(ScanRow).where(ScanRow.scan_id == scan_id))

    def get_owned(self, scan_id: str, owner_id: str) -> ScanRow | None:
        with self._sessions() as session:
            return session.scalar(
                select(ScanRow).where(ScanRow.scan_id == scan_id, ScanRow.owner_id == owner_id)
            )

    def get_findings(self, scan_id: str) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ScanFindingRow)
                .where(ScanFindingRow.scan_id == scan_id)
                .order_by(ScanFindingRow.finding_id)
            )
            findings = [row.payload for row in rows]
            return sorted(
                findings,
                key=lambda finding: (
                    -(
                        finding.get("ai_priority_score")
                        if isinstance(finding.get("ai_priority_score"), int)
                        else finding.get("priority_score", 0)
                    ),
                    -finding.get("priority_score", 0),
                    finding.get("rule_id", ""),
                    finding.get("package", ""),
                ),
            )

    def list_recent(self, owner_id: str, limit: int = 50, offset: int = 0) -> list[ScanRow]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ScanRow)
                    .where(ScanRow.owner_id == owner_id)
                    .order_by(ScanRow.created_at.desc())
                    .limit(max(1, min(limit, 200)))
                    .offset(max(0, offset))
                )
            )

    def list_unfinished(self) -> list[str]:
        """Scans accepted or mid-analysis, oldest first: work a restart would otherwise strand."""
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ScanRow.scan_id)
                    .where(ScanRow.status.in_(("accepted", "analyzing")))
                    .order_by(ScanRow.created_at)
                )
            )

    def find_latest_complete(
        self,
        project_name: str,
        *,
        owner_id: str | None,
        before: datetime,
        exclude_scan_id: str,
        ecosystem: str = "npm",
        package_manager: str = "npm",
        component_path: str = ".",
    ) -> ScanRow | None:
        """The most recent completed scan of the project created strictly before `before`.

        Requiring an earlier scan keeps comparisons pointing forward in time even when scans finish out of order.
        """
        with self._sessions() as session:
            return session.scalar(
                select(ScanRow)
                .where(
                    ScanRow.project_name == project_name,
                    ScanRow.owner_id == owner_id,
                    ScanRow.ecosystem == ecosystem,
                    ScanRow.package_manager == package_manager,
                    ScanRow.component_path == component_path,
                    ScanRow.status == "complete",
                    ScanRow.scan_id != exclude_scan_id,
                    ScanRow.created_at < before,
                )
                .order_by(ScanRow.created_at.desc())
                .limit(1)
            )

    def _update(self, scan_id: str, **values: Any) -> None:
        with self._sessions.begin() as session:
            row = session.get(ScanRow, scan_id)
            if row is None:
                raise KeyError(f"Unknown scan {scan_id}")
            for name, value in values.items():
                setattr(row, name, value)