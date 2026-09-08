"""Star schema for the cargo warehouse.

Three things are enforced here by the database rather than left to
convention, because each one has already gone wrong once in this project:

1. **Provenance is NOT NULL.** Every fact must name the document it came
   from. Source citation is the product's central promise, and a nullable
   foreign key makes it a hope.
2. **Tonnage is non-negative and stored in kilograms.** The unit lives in
   the column name so no caller has to remember it.
3. **The natural key is unique.** Duplicate rows silently double-count a
   city in every ranking, which is exactly the bug the ingestion layer hit
   with `BENGALURU (BIAL)` and again with Frankfurt.

The grain is deliberately explicit. Facts arrive at two grains - airport
per month from AAI and Eurostat, airline per fiscal year from the Open
Government Data platform - and a schema that assumed one would have to
discard the other.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship

# Explicit naming so Alembic autogenerate produces stable, readable names.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


GRAIN = Enum("AIRPORT", "AIRLINE", name="grain_enum")
DIRECTION = Enum("INTERNATIONAL", "DOMESTIC", "TOTAL", name="direction_enum")


# --------------------------------------------------------------- provenance --

class IngestRun(Base):
    """One execution of the pipeline. Lets a whole run be retracted."""

    __tablename__ = "ingest_run"

    ingest_run_id = Column(BigInteger, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    status = Column(String(24), nullable=False, default="RUNNING")
    facts_loaded = Column(Integer, nullable=False, default=0)
    note = Column(Text)

    documents = relationship("SourceDocument", back_populates="run")


class SourceDocument(Base):
    """The artefact a fact was parsed from. This is what makes a citation
    resolvable, and what lets a bad source be retracted in one statement."""

    __tablename__ = "source_document"

    source_document_id = Column(BigInteger, primary_key=True, autoincrement=True)
    doc_key = Column(String(64), nullable=False, unique=True)
    publisher = Column(String(32), nullable=False, index=True)
    title = Column(Text)
    source_url = Column(Text, nullable=False)
    sha256 = Column(String(64))
    media_type = Column(String(64))
    byte_size = Column(Integer)
    raw_path = Column(Text)
    published_on = Column(Date)
    retrieved_at = Column(DateTime(timezone=True), server_default=func.now())
    ingest_run_id = Column(BigInteger, ForeignKey("ingest_run.ingest_run_id"))

    run = relationship("IngestRun", back_populates="documents")
    facts = relationship("CargoFactRow", back_populates="document")

    __table_args__ = (
        # The key travels in the query string for some publishers, so a
        # URL must never be stored raw. This rejects a live key while
        # still accepting the redacted marker - banning `api-key=`
        # outright also rejected `api-key=<redacted>`, which is precisely
        # the form we want stored.
        CheckConstraint(r"source_url !~* 'api[-_]?key=(?!<redacted>)'",
                        name="no_credential_in_url"),
    )


# --------------------------------------------------------------- dimensions --

class DimAirport(Base):
    __tablename__ = "dim_airport"

    airport_id = Column(Integer, primary_key=True, autoincrement=True)
    iata_code = Column(String(3), index=True)
    icao_code = Column(String(8), index=True)
    airport_name = Column(Text, nullable=False)
    city = Column(Text)
    state = Column(Text)
    country = Column(Text, index=True)
    is_international = Column(Boolean, default=False)
    # How this airport was matched, kept so a questionable mapping can be
    # found later rather than being indistinguishable from a certain one.
    resolution_method = Column(String(64))

    __table_args__ = (
        UniqueConstraint("airport_name", "country", name="airport_name_country"),
    )


class DimAirline(Base):
    __tablename__ = "dim_airline"

    airline_id = Column(Integer, primary_key=True, autoincrement=True)
    airline_name = Column(Text, nullable=False, unique=True)
    country = Column(Text)
    # "All Scheduled Indian Airlines" is an industry total, not a carrier.
    # Summing it beside real carriers double-counts the entire market.
    is_aggregate = Column(Boolean, nullable=False, default=False, index=True)


class DimPeriod(Base):
    """Reporting periods, which are not all months.

    Sources publish calendar months ('2026-04'), Indian fiscal years
    ('2015-FY') and plain years ('2023-A'). Flattening those into a single
    date would silently misdate a year of cargo, so the kind is recorded
    and `sort_key` gives a single orderable value across all three.
    """

    __tablename__ = "dim_period"

    period_id = Column(Integer, primary_key=True, autoincrement=True)
    period_label = Column(String(16), nullable=False, unique=True)
    period_kind = Column(String(8), nullable=False)      # MONTH | FISCAL | ANNUAL
    calendar_year = Column(Integer, nullable=False, index=True)
    calendar_month = Column(Integer)
    fiscal_year_start = Column(Integer)
    sort_key = Column(Integer, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("period_kind IN ('MONTH','FISCAL','ANNUAL')",
                        name="period_kind_known"),
        CheckConstraint("calendar_month IS NULL OR calendar_month BETWEEN 1 AND 12",
                        name="month_in_range"),
    )


# --------------------------------------------------------------------- fact --

class CargoFactRow(Base):
    __tablename__ = "fact_cargo_movement"

    fact_id = Column(BigInteger, primary_key=True, autoincrement=True)
    grain = Column(GRAIN, nullable=False, index=True)
    period_id = Column(Integer, ForeignKey("dim_period.period_id"), nullable=False)
    airport_id = Column(Integer, ForeignKey("dim_airport.airport_id"), index=True)
    airline_id = Column(Integer, ForeignKey("dim_airline.airline_id"), index=True)
    direction = Column(DIRECTION, nullable=False)
    tonnage_kg = Column(Numeric(18, 3), nullable=False)
    prior_year_tonnage_kg = Column(Numeric(18, 3))
    reported_change_pct = Column(Float)
    publisher = Column(String(32), nullable=False, index=True)
    # Names the series. One carrier can publish several distinct measures
    # for the same period and direction ("all international scheduled
    # services" is not "international traffic to and from India"), and
    # without this they collapse onto one row and overwrite each other.
    measure = Column(String(64), nullable=False, server_default="freight")
    # Provenance is mandatory, not aspirational.
    source_document_id = Column(
        BigInteger, ForeignKey("source_document.source_document_id"), nullable=False
    )
    resolution_confidence = Column(Float)
    resolution_method = Column(String(64))
    loaded_at = Column(DateTime(timezone=True), server_default=func.now())

    document = relationship("SourceDocument", back_populates="facts")

    __table_args__ = (
        CheckConstraint("tonnage_kg >= 0", name="tonnage_non_negative"),
        # A fact must identify *something*: an airport at airport grain, an
        # airline at airline grain. Neither would make the row meaningless.
        CheckConstraint(
            "(grain = 'AIRPORT' AND airport_id IS NOT NULL) OR "
            "(grain = 'AIRLINE' AND airline_id IS NOT NULL)",
            name="grain_has_its_dimension",
        ),
        # The natural key. Duplicates double-count in every ranking.
        #
        # NULLS NOT DISTINCT is essential rather than cosmetic: by default
        # Postgres treats NULLs as distinct, so an airline-grain row (whose
        # airport_id is NULL) would never conflict with itself and every
        # re-run would insert it again.
        UniqueConstraint(
            "grain", "period_id", "airport_id", "airline_id", "direction",
            "publisher", "measure", name="fact_natural_key",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_fact_period_grain", "period_id", "grain"),
    )


# ------------------------------------------------------------ agent outputs --

class Trend(Base):
    __tablename__ = "trend"

    trend_id = Column(BigInteger, primary_key=True, autoincrement=True)
    grain = Column(GRAIN, nullable=False)
    entity_key = Column(String(64), nullable=False, index=True)
    direction = Column(DIRECTION, nullable=False)
    period_id = Column(Integer, ForeignKey("dim_period.period_id"), nullable=False)
    tonnage_kg = Column(Numeric(18, 3), nullable=False)
    yoy_pct = Column(Float)
    mom_pct = Column(Float)
    cagr_pct = Column(Float)
    share_of_total = Column(Float)
    share_shift_pp = Column(Float)
    computed_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("grain", "entity_key", "direction", "period_id",
                         name="trend_natural_key"),
    )


class Anomaly(Base):
    __tablename__ = "anomaly"

    anomaly_id = Column(BigInteger, primary_key=True, autoincrement=True)
    grain = Column(GRAIN, nullable=False)
    entity_key = Column(String(64), nullable=False, index=True)
    direction = Column(DIRECTION, nullable=False)
    period_id = Column(Integer, ForeignKey("dim_period.period_id"), nullable=False)
    observed_kg = Column(Numeric(18, 3), nullable=False)
    expected_kg = Column(Numeric(18, 3))
    deviation_pct = Column(Float)
    z_score = Column(Float)
    method = Column(String(32), nullable=False)
    severity = Column(String(8), nullable=False)
    # Which fact rows justify this. An anomaly without evidence is a rumour.
    evidence_fact_ids = Column(Text)
    detected_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("severity IN ('LOW','MEDIUM','HIGH')", name="severity_known"),
        UniqueConstraint("grain", "entity_key", "direction", "period_id", "method",
                         name="anomaly_natural_key"),
    )


class Forecast(Base):
    __tablename__ = "forecast"

    forecast_id = Column(BigInteger, primary_key=True, autoincrement=True)
    grain = Column(GRAIN, nullable=False)
    entity_key = Column(String(64), nullable=False, index=True)
    direction = Column(DIRECTION, nullable=False)
    period_label = Column(String(16), nullable=False)
    horizon = Column(Integer, nullable=False)
    predicted_kg = Column(Numeric(18, 3), nullable=False)
    # Intervals are stored because a point estimate with no uncertainty
    # invites more confidence than the data supports.
    lower_kg = Column(Numeric(18, 3))
    upper_kg = Column(Numeric(18, 3))
    model = Column(String(32), nullable=False)
    backtest_mape = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("grain", "entity_key", "direction", "period_label", "model",
                         name="forecast_natural_key"),
    )


class Insight(Base):
    __tablename__ = "insight"

    insight_id = Column(BigInteger, primary_key=True, autoincrement=True)
    headline = Column(Text, nullable=False)
    narrative = Column(Text, nullable=False)
    anomaly_id = Column(BigInteger, ForeignKey("anomaly.anomaly_id"))
    # Every numeric claim must resolve to a stored row.
    citations = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
