"""
Windows policy-based QoS for the audio packets (the GUI's "Wi-Fi priority" button).

Windows ignores the DSCP an application sets on its own sockets; only a QoS
policy can mark its traffic. With one in place the PC -> phone packets carry the
mark through the wired LAN to the Wi-Fi access point, which then sends them in
its voice (WMM) queue instead of queueing them behind other traffic. Creating
the policy needs admin rights once (UAC prompt); it then applies to every new
socket of LANTooth.exe / lantooth-cli.exe.

"Do not use NLA" makes Windows apply local QoS policies on computers that are
not joined to a domain.
"""

import base64
import subprocess
import sys

_POLICIES = (("LANTooth", "LANTooth.exe"), ("LANTooth-CLI", "lantooth-cli.exe"))
_QOS_KEY = r"HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\QoS"


class QosError(Exception):
    pass


def available() -> bool:
    """Policies match the executable name, so they only make sense for the built exe
    (from source the process is python.exe — marking every Python app's UDP)."""
    return bool(getattr(sys, "frozen", False))


def _run_elevated(script: str) -> None:
    encoded = base64.b64encode(("$ErrorActionPreference = 'Stop'\n" + script).encode("utf-16-le")).decode()
    outer = (
        "$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded}'; "
        "exit $p.ExitCode"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", outer],
                           capture_output=True, text=True, timeout=120,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise QosError(str(e)) from e
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()
        raise QosError(detail[-1] if detail else "cancelled or failed (administrator rights are needed)")


def _remove_script() -> str:
    return "".join(
        f"Remove-NetQosPolicy -Name '{name}' -Confirm:$false -ErrorAction SilentlyContinue\n"
        for name, _ in _POLICIES
    )


def enable(dscp: int) -> None:
    """Create (or replace) the QoS policies marking LANTooth's UDP traffic with `dscp`."""
    if not 0 <= dscp <= 63:
        raise ValueError(dscp)
    script = _remove_script() + "".join(
        f"New-NetQosPolicy -Name '{name}' -AppPathNameMatchCondition '{exe}' "
        f"-IPProtocolMatchCondition UDP -DSCPAction {dscp} -NetworkProfile All | Out-Null\n"
        for name, exe in _POLICIES
    ) + (
        f"New-Item -Path '{_QOS_KEY}' -Force | Out-Null\n"
        f"Set-ItemProperty -Path '{_QOS_KEY}' -Name 'Do not use NLA' -Value '1' -Type String\n"
    )
    _run_elevated(script)


def disable() -> None:
    _run_elevated(_remove_script())
