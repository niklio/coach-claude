"""Apple Health integration stub.

Apple Health data is accessed through Apple's HealthKit framework, which is only
available to native iOS apps built with Swift/Objective-C using WKWebView or a
dedicated app target. HealthKit cannot be accessed from a web browser — there is
no REST API, no OAuth flow, and no browser-based access method provided by Apple.

To support Apple Health in the future, a native iOS companion app would need to be
built that reads HealthKit data on-device and forwards it to this backend.
"""


class IntegrationNotAvailableError(Exception):
    """Raised when an integration cannot be supported on this platform."""


def get_auth_url(*args, **kwargs) -> str:
    raise IntegrationNotAvailableError(
        "Apple Health is not available via web. HealthKit requires a native iOS app "
        "built with Swift — it cannot be accessed from a web browser. "
        "A native iOS companion app would be needed to support Apple Health."
    )


def get_workouts(*args, **kwargs) -> list:
    raise IntegrationNotAvailableError(
        "Apple Health is not available via web. HealthKit requires a native iOS app "
        "built with Swift — it cannot be accessed from a web browser. "
        "A native iOS companion app would be needed to support Apple Health."
    )
