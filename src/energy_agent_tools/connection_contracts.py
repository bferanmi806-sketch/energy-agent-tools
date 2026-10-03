"""Public metadata and bounded requests for gateway connection onboarding."""

from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .models import StrictModel


class _ConnectionModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConnectionField(_ConnectionModel):
    name: Literal["credential", "mpan", "serial_number"]
    label: StrictStr
    secret: StrictBool
    min_length: StrictInt
    max_length: StrictInt
    pattern: StrictStr | None = None


class ConnectionSetup(_ConnectionModel):
    toolkit_id: StrictStr
    provider: Literal["octopus"]
    description: StrictStr
    enabled: StrictBool
    unavailable_reason: Literal["management_key_required", "storage_unavailable"] | None = None
    fields: list[ConnectionField]


class ConnectionSetupsResponse(_ConnectionModel):
    setups: list[ConnectionSetup]


class OctopusConnectionRequest(_ConnectionModel):
    provider: Literal["octopus"]
    credential: StrictStr = Field(min_length=1, max_length=4096, repr=False)
    mpan: StrictStr = Field(pattern=r"^[0-9]{13}$")
    serial_number: StrictStr = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._~-]+$")


def octopus_setup(
    *,
    enabled: bool,
    unavailable_reason: Literal["management_key_required", "storage_unavailable"] | None = None,
) -> ConnectionSetup:
    return ConnectionSetup(
        toolkit_id="octopus-energy-account",
        provider="octopus",
        description="Verify your Octopus API key and electricity meter, then connect it to the selected site.",
        enabled=enabled,
        unavailable_reason=unavailable_reason,
        fields=[
            ConnectionField(
                name="credential",
                label="Octopus API key",
                secret=True,
                min_length=1,
                max_length=4096,
            ),
            ConnectionField(
                name="mpan",
                label="Electricity MPAN",
                secret=False,
                min_length=13,
                max_length=13,
                pattern=r"[0-9]{13}",
            ),
            ConnectionField(
                name="serial_number",
                label="Meter serial number",
                secret=False,
                min_length=1,
                max_length=120,
                pattern=r"[A-Za-z0-9._~\-]+",
            ),
        ],
    )
