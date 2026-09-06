import ctypes
import ctypes.util
import os
import sys

EPERM = 1
PR_SET_NO_NEW_PRIVS = 38
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO_BASE = 0x00050000
SCMP_ACT_KILL_PROCESS = 0x80300000

DENIED_SYSCALLS = (
    "reboot", "swapon", "swapoff", "setns", "unshare", "chroot", "pivot_root", "mount", "umount2",
    "acct", "init_module", "finit_module", "delete_module", "quotactl", "kexec_load", "kexec_file_load",
    "ptrace", "process_vm_readv", "process_vm_writev", "open_by_handle_at", "name_to_handle_at",
    "perf_event_open", "bpf", "userfaultfd", "syslog", "settimeofday", "clock_settime", "adjtimex",
    "clock_adjtime", "sethostname", "setdomainname", "personality", "uselib", "ustat", "lookup_dcookie",
)


class SandboxUnavailable(RuntimeError):
    pass


def require_linux():
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable("The compute worker sandbox requires Linux")


def libseccomp():
    path = ctypes.util.find_library("seccomp")
    if path is None:
        for candidate in ("libseccomp.so.2",):
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                continue
        raise SandboxUnavailable("libseccomp2 is required by the compute worker")
    try:
        return ctypes.CDLL(path)
    except OSError:
        raise SandboxUnavailable("libseccomp2 is required by the compute worker") from None


def set_no_new_privileges():
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    except (OSError, AttributeError):
        result = -1
    if result != 0:
        raise SandboxUnavailable("The kernel refused PR_SET_NO_NEW_PRIVS")


def apply_syscall_denylist(denied=DENIED_SYSCALLS):
    require_linux()
    library = libseccomp()
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    context = library.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise SandboxUnavailable("seccomp_init failed")
    applied = 0
    try:
        for name in denied:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            result = library.seccomp_rule_add(context, SCMP_ACT_ERRNO_BASE | EPERM, number, 0)
            if result < 0:
                raise SandboxUnavailable(f"seccomp_rule_add failed for {name}")
            applied += 1
        if applied == 0:
            raise SandboxUnavailable("libseccomp resolved no sandbox syscalls for this architecture")
        if library.seccomp_load(context) < 0:
            raise SandboxUnavailable("seccomp_load failed")
    finally:
        library.seccomp_release(context)
    return {"applied_rules": applied}


def verify_denylist():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sethostname.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(b"agrocast-sandbox-probe")
    ctypes.set_errno(0)
    result = libc.sethostname(buffer, 1)
    errno = ctypes.get_errno()
    if result == -1 and errno == EPERM:
        return {"ok": True}
    if result == 0:
        raise SandboxUnavailable("The seccomp denylist did not block sethostname")
    raise SandboxUnavailable("sethostname probe failed with an unexpected errno")


def apply_limits(deadline_seconds, fsize_mb=256, cpu_margin=60, max_rss_mb=0):
    import resource

    applied = {}
    soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    limit = int(deadline_seconds) + int(cpu_margin)
    if max_rss_mb:
        bytes_limit = int(max_rss_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
        applied["rss_mb"] = int(max_rss_mb)
    if hard in (resource.RLIM_INFINITY,) or hard > limit:
        resource.setrlimit(resource.RLIMIT_CPU, (limit, limit + 10))
    applied["cpu_seconds"] = limit
    if fsize_mb:
        bytes_limit = int(fsize_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (bytes_limit, bytes_limit))
        applied["fsize_mb"] = int(fsize_mb)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return applied


def thread_limit_environment(threads):
    names = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
    return {name: str(int(threads)) for name in names}


def effective_thread_report():
    report = {}
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        report[name] = os.environ.get(name)
    report["cpu_affinity"] = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return report
