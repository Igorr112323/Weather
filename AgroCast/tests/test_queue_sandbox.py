import ctypes
import sys
from pathlib import Path

import pytest

from agrocast.queue import sandbox


class RecordingLibrary:
    def __init__(self):
        self.members = {}

    def __getattr__(self, name):
        if name in self.members:
            return self.members[name]

        def call(*args):
            call.calls.append(args)
            return {
                "seccomp_init": 0x7FFFFFFF12345678,
                "seccomp_syscall_resolve_name": 42,
            }.get(name, 0)

        call.calls = []
        self.members[name] = call
        return call


def test_seccomp_abi_declares_pointer_arguments():
    recording = RecordingLibrary()
    original = sandbox.libseccomp
    sandbox.libseccomp = lambda: recording
    try:
        sandbox.apply_syscall_denylist()
    finally:
        sandbox.libseccomp = original
    for name in ("seccomp_init", "seccomp_rule_add", "seccomp_load", "seccomp_release"):
        member = recording.members[name]
        assert member.argtypes is not None, name
        assert member.restype is not None or name == "seccomp_release", name
    assert recording.members["seccomp_rule_add"].argtypes[0] is ctypes.c_void_p
    assert recording.members["seccomp_load"].argtypes == [ctypes.c_void_p]
    assert recording.members["seccomp_release"].argtypes == [ctypes.c_void_p]
    assert recording.members["seccomp_init"].restype is ctypes.c_void_p
    context_passed = {call[0] for call in recording.members["seccomp_rule_add"].calls}
    assert context_passed == {0x7FFFFFFF12345678}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_NOFILE is POSIX")
def test_nofile_cap_binds_below_a_generous_host_default():
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import resource\n"
        "from agrocast.queue.sandbox import NOFILE_CAP, apply_limits\n"
        "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
        "base = 65536 if (hard == resource.RLIM_INFINITY or hard >= 65536) else hard\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (base, hard))\n"
        "apply_limits(60)\n"
        "new_soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]\n"
        "assert new_soft == min(base, NOFILE_CAP), new_soft\n"
        "print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(repo_root), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="libseccomp is Linux only")
def test_denylist_application_survives_high_address_context():
    import subprocess

    code = (
        "from agrocast.queue.sandbox import apply_syscall_denylist, verify_denylist\n"
        "print(apply_syscall_denylist()['applied_rules'])\n"
        "verify_denylist()\n"
        "print('ok')"
    )
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", code], cwd=str(repo_root), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
