"""Kart setup configuration schema.

Captures gearing, carburettor, tyre, and chassis settings plus track/session
context, none of which exist in the Unipro TSV -- all of it is user-supplied,
stored as one YAML file per session so it can be cross-referenced against
that session's telemetry by `setup_engine.py`.

Fields default toward Rotax Senior EVO / Dellorto VHSB34 conventions (single
speed, direct drive) but stay generic enough to reuse for other classes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import yaml


@dataclass
class Gearing:
    front_teeth: int | None = None
    rear_teeth: int | None = None
    chain_pitch: str = "219"
    axle_type: str = "fixed"  # "fixed" | "floating"
    axle_diameter_mm: float | None = None
    axle_stiffness: str | None = None  # e.g. "soft" | "medium" | "hard"

    @property
    def gear_ratio(self) -> float | None:
        if self.front_teeth and self.rear_teeth:
            return self.rear_teeth / self.front_teeth
        return None


@dataclass
class Carburettor:
    model: str = "Dellorto VHSB34"
    main_jet: int | None = 128
    idle_jet: int | None = None
    needle_type: str | None = None
    needle_clip_position: int | None = None
    air_screw_turns_out: float | None = None
    float_height_mm: float | None = None
    altitude_m: float | None = None
    ambient_temp_c: float | None = None
    track_temp_c: float | None = None


@dataclass
class Tyres:
    compound: str | None = None
    hot_pressure_front_bar: float | None = None
    hot_pressure_rear_bar: float | None = None
    cold_pressure_front_bar: float | None = None
    cold_pressure_rear_bar: float | None = None
    age_laps: int | None = None


@dataclass
class Chassis:
    front_track_width_mm: float | None = None
    rear_track_width_mm: float | None = None
    caster: float | None = None
    camber: float | None = None
    seat_position_fore_aft_mm: float | None = None
    seat_position_height_mm: float | None = None
    ride_height_mm: float | None = None
    torsion_bar: str | None = None
    engine_mounting_position: str | None = None
    weight_distribution_notes: str | None = None
    ballast_kg: float | None = None


@dataclass
class TrackSessionContext:
    track_name: str | None = None
    layout_direction: str | None = None
    weather: str | None = None
    track_temp_c: float | None = None
    session_type: str = "practice"  # "practice" | "qualifying" | "race"
    date: str | None = None
    notes: str | None = None


@dataclass
class KartSetup:
    driver: str | None = None
    class_name: str = "Rotax Senior EVO"
    # Engine's peak-power RPM band -- a parameter, not a hard-coded constant,
    # since it varies by engine build/tune. Rotax Senior EVO default is a
    # reasonable starting point; confirm against the actual engine builder's
    # spec sheet and adjust here.
    peak_power_rpm_low: int = 11500
    peak_power_rpm_high: int = 13000
    gearing: Gearing = field(default_factory=Gearing)
    carburettor: Carburettor = field(default_factory=Carburettor)
    tyres: Tyres = field(default_factory=Tyres)
    chassis: Chassis = field(default_factory=Chassis)
    track_session: TrackSessionContext = field(default_factory=TrackSessionContext)

    def to_yaml(self, path: str) -> None:
        with open(path, "w") as f:
            yaml.safe_dump(asdict(self), f, sort_keys=False)

    @classmethod
    def from_yaml(cls, path: str) -> "KartSetup":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "KartSetup":
        return cls(
            driver=data.get("driver"),
            class_name=data.get("class_name", "Rotax Senior EVO"),
            peak_power_rpm_low=data.get("peak_power_rpm_low", 11500),
            peak_power_rpm_high=data.get("peak_power_rpm_high", 13000),
            gearing=Gearing(**data.get("gearing", {})),
            carburettor=Carburettor(**data.get("carburettor", {})),
            tyres=Tyres(**data.get("tyres", {})),
            chassis=Chassis(**data.get("chassis", {})),
            track_session=TrackSessionContext(**data.get("track_session", {})),
        )

    def to_dict(self) -> dict:
        return asdict(self)
