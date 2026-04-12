#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path


BUNDLE_RE = re.compile(r"/([^/]+)\.app/")
INTERPRETERS = {
    "python",
    "python3",
    "node",
    "bun",
    "ruby",
    "php",
    "perl",
    "java",
    "bash",
    "zsh",
    "sh",
}
SYSTEM_USERS = {"root"}
GENERIC_SYSTEM_NOTE = "macOS system service."
GENERIC_DEV_NOTE = (
    "Looks like a developer tool, local server, or script you started yourself."
)
GENERIC_HELPER_NOTE = "Helper process from a larger app. Group it with the parent app rather than judging it alone."
SECRET_ARG_RE = re.compile(
    r"(?i)(?P<prefix>--?[a-z0-9_-]*(?:token|secret|password|passwd|api[-_]?key|credential|session|cookie)[a-z0-9_-]*(?:=|\s+))(?P<value>\S+)"
)
SECRET_ENV_RE = re.compile(
    r"(?i)\b(?P<prefix>[a-z0-9_]*(?:token|secret|password|passwd|api_key|credential|session|cookie)[a-z0-9_]*=)(?P<value>\S+)"
)
URL_CREDENTIAL_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
USER_HOME_RE = re.compile(r"/Users/[^/\s]+/")


def run_command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown error"
        raise RuntimeError(f"{' '.join(args)} failed: {stderr}")
    return result.stdout


def parse_sw_vers(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def parse_vm_stat(text: str) -> dict[str, object]:
    match = re.search(r"page size of (\d+) bytes", text)
    if not match:
        raise RuntimeError("Could not parse vm_stat page size")

    page_size = int(match.group(1))
    stats: dict[str, int] = {}

    for line in text.splitlines():
        line = line.strip()
        stat_match = re.match(r'"?([^:]+)"?:\s+([0-9]+)\.?$', line)
        if not stat_match:
            continue
        key = stat_match.group(1).strip().strip('"').lower().replace(" ", "_")
        stats[key] = int(stat_match.group(2))

    return {"page_size": page_size, "stats": stats}


def parse_memory_pressure(text: str) -> dict[str, int | None]:
    free_pct_match = re.search(r"System-wide memory free percentage:\s+(\d+)%", text)
    total_bytes_match = re.search(r"The system has\s+(\d+)", text)
    return {
        "free_percentage": int(free_pct_match.group(1)) if free_pct_match else None,
        "reported_total_bytes": int(total_bytes_match.group(1))
        if total_bytes_match
        else None,
    }


def parse_top(text: str) -> dict[str, object]:
    cpu_match = re.search(
        r"CPU usage:\s+([0-9.]+)% user,\s+([0-9.]+)% sys,\s+([0-9.]+)% idle", text
    )
    load_match = re.search(r"Load Avg:\s+([0-9.]+),\s+([0-9.]+),\s+([0-9.]+)", text)

    if not cpu_match:
        raise RuntimeError("Could not parse top CPU summary")

    user = float(cpu_match.group(1))
    system = float(cpu_match.group(2))
    idle = float(cpu_match.group(3))

    return {
        "user_pct": user,
        "system_pct": system,
        "idle_pct": idle,
        "busy_pct": max(0.0, 100.0 - idle),
        "load_avg": [float(load_match.group(i)) for i in range(1, 4)]
        if load_match
        else None,
    }


def parse_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def bundle_name(command: str) -> str | None:
    if not command.startswith(
        (
            "/Applications/",
            "/System/Applications/",
            "/System/Library/CoreServices/",
        )
    ):
        return None

    match = BUNDLE_RE.search(command)
    return match.group(1) if match else None


def normalize_family(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("google chrome"):
        return "Google Chrome"
    if lowered.startswith("microsoft edge"):
        return "Microsoft Edge"
    if lowered.startswith("cursor"):
        return "Cursor"
    if lowered.startswith("discord"):
        return "Discord"
    if lowered.startswith("spotify"):
        return "Spotify"
    if lowered.startswith("superhuman"):
        return "Superhuman"
    if lowered.startswith("raycast"):
        return "Raycast"
    if lowered.startswith("wispr flow"):
        return "Wispr Flow"
    if lowered.startswith("orbstack"):
        return "OrbStack"
    if lowered.startswith("chrome-headless-shell"):
        return "chrome-headless-shell"

    cleaned = re.sub(r"\s+Helper.*$", "", name).strip()
    return cleaned or name


def display_name(executable_name: str, command: str) -> str:
    bundle = bundle_name(command)
    cleaned_name = executable_name.strip()
    if cleaned_name:
        return cleaned_name

    tokens = parse_tokens(command)

    if tokens:
        first = tokens[0]
        if first.startswith("/"):
            base = Path(first).name
            if base:
                return base
        if first in INTERPRETERS and len(tokens) > 1 and tokens[1].startswith("/"):
            return f"{first} {Path(tokens[1]).name}"
        return first

    return bundle or command[:40]


def family_name(executable_name: str, command: str) -> str:
    bundle = bundle_name(command)
    if bundle:
        return bundle

    cleaned_name = executable_name.strip()
    if cleaned_name:
        normalized_name = normalize_family(cleaned_name)
        if normalized_name.lower() not in INTERPRETERS:
            return normalized_name

    tokens = parse_tokens(command)
    if not tokens:
        return "unknown"

    first = tokens[0]
    if first in INTERPRETERS and len(tokens) > 1 and tokens[1].startswith("/"):
        target = Path(tokens[1]).name
        if target.endswith((".py", ".js", ".ts", ".mjs", ".cjs", ".tsx", ".jsx")):
            return target.rsplit(".", 1)[0]

    if first.startswith("/"):
        return normalize_family(Path(first).name)

    return normalize_family(first)


def is_system_process(user: str, command: str) -> bool:
    if user in SYSTEM_USERS or user.startswith("_"):
        return True
    return (
        command.startswith("/System/")
        or command.startswith("/usr/")
        or command.startswith("/sbin/")
    )


def is_dev_process(family: str, command: str) -> bool:
    family_lower = family.lower()
    command_lower = command.lower()
    if family_lower in {
        "node",
        "bun",
        "python",
        "python3",
        "tsserver",
        "vite",
        "esbuild",
        "webpack",
    }:
        return True
    markers = [
        "node_modules",
        "typescript-language-server",
        "tsserver",
        "vite",
        "webpack",
        "esbuild",
        "convex",
        "npm exec",
        "uv run",
        "/dev/",
    ]
    return any(marker in command_lower for marker in markers)


def note_for(family: str, command: str, user: str) -> str | None:
    family_lower = family.lower()
    command_lower = command.lower()

    if family_lower == "kernel_task":
        return "macOS kernel. High CPU here often means heat control or a driver issue, not a normal app doing work."
    if family_lower == "windowserver":
        return "Draws windows, displays, and animations. High usage usually means lots of windows, video, or external monitors."
    if family_lower in {
        "mds",
        "mds_stores",
        "mdworker",
        "mdworker_shared",
        "spotlight",
        "spotlightknowledged",
        "corespotlightd",
    }:
        return "Spotlight indexing or search work. Spikes are common after updates or lots of file changes."
    if family_lower in {"backupd"}:
        return "Time Machine backup work."
    if family_lower in {"bird", "cloudd", "nsurlsessiond"}:
        return (
            "Sync or background transfer work, often iCloud or app uploads/downloads."
        )
    if family_lower in {"photolibraryd"}:
        return "Photos indexing or iCloud Photos syncing."
    if family_lower in {"coreaudiod"}:
        return "macOS audio service. It can spike during calls, recording, or if an audio device/app is misbehaving."
    if family_lower in {"corespeechd", "assistantd", "siriactionsd", "sirittsd"}:
        return "Speech, dictation, or Siri-related processing."
    if family_lower in {"softwareupdated"}:
        return "macOS update service. Usually temporary."
    if family_lower in {"syspolicyd", "xprotectservice", "xprotectbridgeservice"}:
        return "Security scanning or app verification work. Spikes are common after downloads or app launches."
    if family_lower == "orbstack":
        return "Container or virtual machine workload from OrbStack."
    if family_lower in {"google chrome", "microsoft edge", "chrome-headless-shell"}:
        return "Browser work is split across many helper processes, so judge it by the grouped app total."

    if ".app/" in command and "helper" in command_lower:
        return GENERIC_HELPER_NOTE
    if is_dev_process(family, command):
        return GENERIC_DEV_NOTE
    if is_system_process(user, command):
        return GENERIC_SYSTEM_NOTE
    return None


def note_rank(note: str | None) -> int:
    if not note:
        return 0
    if note == GENERIC_SYSTEM_NOTE:
        return 1
    if note == GENERIC_DEV_NOTE:
        return 2
    return 3


def app_note(note: str | None) -> str | None:
    if note == GENERIC_HELPER_NOTE:
        return None
    return note


def classify_process(family: str, command: str, user: str) -> str:
    if is_system_process(user, command):
        return "system"
    if is_dev_process(family, command):
        return "developer"
    if ".app/" in command:
        return "app"
    return "service"


def command_preview(command: str, limit: int = 120) -> str:
    redacted = redact_command(command)
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 3] + "..."


def redact_command(command: str) -> str:
    redacted = USER_HOME_RE.sub("~/", command)
    redacted = URL_CREDENTIAL_RE.sub("://[redacted]@", redacted)
    redacted = SECRET_ARG_RE.sub(
        lambda match: f"{match.group('prefix')}[redacted]", redacted
    )
    return SECRET_ENV_RE.sub(
        lambda match: f"{match.group('prefix')}[redacted]", redacted
    )


def parse_ucomm_map(text: str) -> dict[int, str]:
    names_by_pid: dict[int, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = raw_line.split(None, 1)
        if len(parts) != 2:
            continue

        names_by_pid[int(parts[0])] = parts[1].strip()

    return names_by_pid


def parse_processes(
    text: str, executable_names: dict[int, str], include_full_command: bool = False
) -> list[dict[str, object]]:
    current_pid = os.getpid()
    processes: list[dict[str, object]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = raw_line.split(None, 6)
        if len(parts) != 7:
            continue

        pid = int(parts[0])
        ppid = int(parts[1])
        user = parts[2]
        cpu_pct = float(parts[3])
        rss_kb = int(parts[4])
        state = parts[5]
        command = parts[6].strip()
        executable_name = executable_names.get(pid, "")

        if pid == current_pid:
            continue
        if "check_mac_resources.py" in command:
            continue
        if command.startswith("ps "):
            continue

        family = family_name(executable_name, command)
        process = {
            "pid": pid,
            "ppid": ppid,
            "user": user,
            "cpu_pct": cpu_pct,
            "rss_kb": rss_kb,
            "rss_bytes": rss_kb * 1024,
            "state": state,
            "family": family,
            "name": display_name(executable_name, command),
            "kind": classify_process(family, command, user),
            "note": note_for(family, command, user),
            "command_preview": command_preview(command),
        }
        if include_full_command:
            process["command"] = command

        processes.append(process)

    return processes


def aggregate_apps(processes: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}

    for process in processes:
        family = str(process["family"])
        app = grouped.setdefault(
            family,
            {
                "family": family,
                "cpu_pct": 0.0,
                "rss_bytes": 0,
                "process_count": 0,
                "kind": process["kind"],
                "kind_counts": {"app": 0, "developer": 0, "service": 0, "system": 0},
                "note": app_note(process["note"]),
                "top_process_name": process["name"],
                "top_process_cpu_pct": process["cpu_pct"],
                "top_process_rss_bytes": process["rss_bytes"],
                "sample_command": process["command_preview"],
            },
        )

        app["cpu_pct"] = float(app["cpu_pct"]) + float(process["cpu_pct"])
        app["rss_bytes"] = int(app["rss_bytes"]) + int(process["rss_bytes"])
        app["process_count"] = int(app["process_count"]) + 1
        app["kind_counts"][str(process["kind"])] = (
            int(app["kind_counts"][str(process["kind"])]) + 1
        )

        if float(process["cpu_pct"]) > float(app["top_process_cpu_pct"]):
            app["top_process_cpu_pct"] = process["cpu_pct"]
            app["top_process_name"] = process["name"]
            app["sample_command"] = process["command_preview"]

        if int(process["rss_bytes"]) > int(app["top_process_rss_bytes"]):
            app["top_process_rss_bytes"] = process["rss_bytes"]

        candidate_note = app_note(process["note"])
        if note_rank(candidate_note) > note_rank(app["note"]):
            app["note"] = candidate_note

    apps = list(grouped.values())
    for app in apps:
        kind_counts = dict(app["kind_counts"])
        if int(kind_counts.get("app", 0)) > 0:
            app["kind"] = "app"
        elif int(kind_counts.get("developer", 0)) > 0:
            app["kind"] = "developer"
        elif int(kind_counts.get("service", 0)) > 0:
            app["kind"] = "service"
        else:
            app["kind"] = "system"

        if app["kind"] != "system" and app["note"] == GENERIC_SYSTEM_NOTE:
            app["note"] = None

        del app["kind_counts"]

    return apps


def format_bytes(num_bytes: int) -> str:
    units = [
        (1024**4, "TiB"),
        (1024**3, "GiB"),
        (1024**2, "MiB"),
        (1024, "KiB"),
    ]
    for size, suffix in units:
        if num_bytes >= size:
            return f"{num_bytes / size:.1f} {suffix}"
    return f"{num_bytes} B"


def percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round((float(part) / float(total)) * 100.0, 1)


def enrich_resource_shares(
    items: list[dict[str, object]], total_memory_bytes: int, logical_cpu_count: int
) -> None:
    for item in items:
        item["memory_share_pct"] = percent(int(item["rss_bytes"]), total_memory_bytes)
        item["cpu_cores_equivalent"] = round(float(item["cpu_pct"]) / 100.0, 2)
        item["cpu_total_capacity_pct"] = percent(
            float(item["cpu_pct"]), max(logical_cpu_count * 100.0, 1.0)
        )


def memory_snapshot(
    total_bytes: int, vm: dict[str, object], pressure: dict[str, int | None]
) -> dict[str, object]:
    page_size = int(vm["page_size"])
    stats = dict(vm["stats"])

    free_pages = stats.get("pages_free", 0)
    inactive_pages = stats.get("pages_inactive", 0)
    speculative_pages = stats.get("pages_speculative", 0)
    purgeable_pages = stats.get("pages_purgeable", 0)
    active_pages = stats.get("pages_active", 0)
    wired_pages = stats.get("pages_wired_down", 0)
    compressed_pages = stats.get("pages_occupied_by_compressor", 0)

    free_bytes = (free_pages + speculative_pages + purgeable_pages) * page_size
    reclaimable_bytes = (
        free_pages + inactive_pages + speculative_pages + purgeable_pages
    ) * page_size
    active_bytes = active_pages * page_size
    wired_bytes = wired_pages * page_size
    compressed_bytes = compressed_pages * page_size
    used_estimate_bytes = max(total_bytes - reclaimable_bytes, 0)

    free_pct = pressure.get("free_percentage")
    compressed_ratio = compressed_bytes / total_bytes if total_bytes else 0.0
    compressed_pct = percent(compressed_bytes, total_bytes)
    pressure_reasons: list[str] = []
    status_score = 0

    if free_pct is not None:
        if free_pct < 8:
            status_score = max(status_score, 3)
            pressure_reasons.append(f"memory_pressure reports only {free_pct}% free")
        elif free_pct < 12:
            status_score = max(status_score, 2)
            pressure_reasons.append(f"memory_pressure reports {free_pct}% free")
        elif free_pct < 20:
            status_score = max(status_score, 1)
            pressure_reasons.append(f"memory_pressure reports {free_pct}% free")

    if compressed_ratio >= 0.35:
        status_score = max(status_score, 2)
        pressure_reasons.append(f"compressed memory is {compressed_pct}% of RAM")
    elif compressed_ratio >= 0.15:
        status_score = max(status_score, 1)
        pressure_reasons.append(f"compressed memory is {compressed_pct}% of RAM")

    if status_score == 3:
        status = "tight"
    elif status_score == 2:
        status = "under_pressure"
    elif status_score == 1:
        status = "busy"
    else:
        status = "comfortable"
        pressure_reasons.append(
            "free memory and compressed memory are within comfortable thresholds"
        )

    return {
        "status": status,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "reclaimable_bytes": reclaimable_bytes,
        "used_estimate_bytes": used_estimate_bytes,
        "active_bytes": active_bytes,
        "wired_bytes": wired_bytes,
        "compressed_bytes": compressed_bytes,
        "free_pct_estimate": percent(free_bytes, total_bytes),
        "reclaimable_pct": percent(reclaimable_bytes, total_bytes),
        "used_estimate_pct": percent(used_estimate_bytes, total_bytes),
        "active_pct": percent(active_bytes, total_bytes),
        "wired_pct": percent(wired_bytes, total_bytes),
        "compressed_pct": compressed_pct,
        "memory_pressure_free_pct": free_pct,
        "pressure_reasons": pressure_reasons,
        "swapins": stats.get("swapins"),
        "swapouts": stats.get("swapouts"),
        "pageins": stats.get("pageins"),
        "pageouts": stats.get("pageouts"),
    }


def cpu_status(cpu: dict[str, object]) -> str:
    busy_pct = float(cpu["busy_pct"])
    if busy_pct >= 80:
        return "hot"
    if busy_pct >= 50:
        return "busy"
    if busy_pct >= 25:
        return "active"
    return "calm"


def memory_status_text(status: str) -> str:
    if status == "under_pressure":
        return "under pressure"
    return status


def build_insights(
    cpu: dict[str, object],
    memory: dict[str, object],
    top_apps_by_cpu: list[dict[str, object]],
    top_apps_by_memory: list[dict[str, object]],
    logical_cpu_count: int,
) -> list[dict[str, object]]:
    insights: list[dict[str, object]] = []
    memory_status = str(memory["status"])
    top_cpu_app = top_apps_by_cpu[0] if top_apps_by_cpu else None
    top_mem_app = top_apps_by_memory[0] if top_apps_by_memory else None

    if memory_status in {"tight", "under_pressure", "busy"}:
        severity = "critical" if memory_status == "tight" else "warning"
        if memory_status == "busy":
            severity = "notice"
        insights.append(
            {
                "id": "memory_pressure",
                "category": "memory",
                "severity": severity,
                "title": "Memory pressure signals are elevated",
                "evidence": {
                    "status": memory_status,
                    "used_estimate_pct": memory["used_estimate_pct"],
                    "reclaimable_pct": memory["reclaimable_pct"],
                    "compressed_pct": memory["compressed_pct"],
                    "memory_pressure_free_pct": memory["memory_pressure_free_pct"],
                    "pressure_reasons": memory["pressure_reasons"],
                },
                "next_step": "Start with the largest non-system RAM users, then re-run the check to see whether compressed memory drops.",
            }
        )

    cpu_state = cpu_status(cpu)
    if cpu_state in {"hot", "busy"}:
        insights.append(
            {
                "id": "cpu_busy",
                "category": "cpu",
                "severity": "warning" if cpu_state == "hot" else "notice",
                "title": "CPU is meaningfully busy",
                "evidence": {
                    "busy_pct": round(float(cpu["busy_pct"]), 1),
                    "user_pct": round(float(cpu["user_pct"]), 1),
                    "system_pct": round(float(cpu["system_pct"]), 1),
                    "top_cpu_app": top_cpu_app["family"] if top_cpu_app else None,
                    "top_cpu_app_pct": round(float(top_cpu_app["cpu_pct"]), 1)
                    if top_cpu_app
                    else None,
                },
                "next_step": "Check whether the top CPU app is expected to be active; quit or pause it if the load is unexpected.",
            }
        )

    if top_cpu_app:
        cpu_cores = float(top_cpu_app.get("cpu_cores_equivalent", 0.0))
        if cpu_cores >= 0.25:
            insights.append(
                {
                    "id": "top_cpu_app",
                    "category": "app",
                    "severity": "info",
                    "title": "One app family is the current CPU leader",
                    "evidence": {
                        "family": top_cpu_app["family"],
                        "kind": top_cpu_app["kind"],
                        "cpu_pct": round(float(top_cpu_app["cpu_pct"]), 1),
                        "cpu_cores_equivalent": cpu_cores,
                        "process_count": top_cpu_app["process_count"],
                        "sample_command": top_cpu_app["sample_command"],
                    },
                    "next_step": "Treat helper processes as part of this grouped app total.",
                }
            )

    if top_mem_app:
        memory_share = float(top_mem_app.get("memory_share_pct", 0.0))
        if memory_share >= 5.0:
            insights.append(
                {
                    "id": "top_memory_app",
                    "category": "app",
                    "severity": "info",
                    "title": "One app family is the current RAM leader",
                    "evidence": {
                        "family": top_mem_app["family"],
                        "kind": top_mem_app["kind"],
                        "rss_bytes": top_mem_app["rss_bytes"],
                        "memory_share_pct": memory_share,
                        "process_count": top_mem_app["process_count"],
                        "sample_command": top_mem_app["sample_command"],
                    },
                    "next_step": "If memory pressure matters right now, restart or reduce the workload in this app before chasing small helper processes.",
                }
            )

    developer_apps = [
        app
        for app in top_apps_by_cpu + top_apps_by_memory
        if app.get("kind") == "developer"
        and (float(app["cpu_pct"]) >= 10.0 or int(app["rss_bytes"]) >= 1024**3)
    ]
    seen_developer_families: set[str] = set()
    notable_developer_apps = []
    for app in developer_apps:
        family = str(app["family"])
        if family in seen_developer_families:
            continue
        seen_developer_families.add(family)
        notable_developer_apps.append(
            {
                "family": family,
                "cpu_pct": round(float(app["cpu_pct"]), 1),
                "rss_bytes": app["rss_bytes"],
                "memory_share_pct": app.get("memory_share_pct"),
                "sample_command": app.get("sample_command"),
            }
        )

    if notable_developer_apps:
        insights.append(
            {
                "id": "developer_workload",
                "category": "developer",
                "severity": "notice",
                "title": "Developer processes are a visible part of the load",
                "evidence": {"apps": notable_developer_apps},
                "next_step": "Look for watch servers, local dev services, containers, or test runners that are still running after you stopped using them.",
            }
        )

    renderer_heavy_apps = [
        {
            "family": app["family"],
            "process_count": app["process_count"],
            "rss_bytes": app["rss_bytes"],
            "memory_share_pct": app.get("memory_share_pct"),
        }
        for app in top_apps_by_memory
        if int(app["process_count"]) >= 20 and int(app["rss_bytes"]) >= 1024**3
    ]
    if renderer_heavy_apps:
        insights.append(
            {
                "id": "many_helper_processes",
                "category": "app",
                "severity": "info",
                "title": "Some app totals are spread across many helpers",
                "evidence": {"apps": renderer_heavy_apps},
                "next_step": "Use grouped app totals for browsers and Electron apps; individual renderer rows can be misleading.",
            }
        )

    load_avg = cpu.get("load_avg")
    if isinstance(load_avg, list) and load_avg and logical_cpu_count:
        load_per_cpu = round(float(load_avg[0]) / logical_cpu_count, 2)
        cpu["load_1m_per_logical_cpu"] = load_per_cpu
        if load_per_cpu >= 1.0:
            insights.append(
                {
                    "id": "load_queue",
                    "category": "cpu",
                    "severity": "warning",
                    "title": "Runnable work is queued across the CPU pool",
                    "evidence": {
                        "load_1m": load_avg[0],
                        "logical_cpu_count": logical_cpu_count,
                        "load_1m_per_logical_cpu": load_per_cpu,
                    },
                    "next_step": "Find sustained CPU users rather than short spikes; the process list is a snapshot.",
                }
            )

    return insights


def plain_summary(
    cpu: dict[str, object],
    memory: dict[str, object],
    top_cpu_app: dict[str, object] | None,
    top_mem_app: dict[str, object] | None,
) -> str:
    cpu_state = cpu_status(cpu)
    memory_state = memory_status_text(str(memory["status"]))
    parts = [
        f"CPU is {cpu_state} at {float(cpu['busy_pct']):.1f}% busy.",
        f"Memory is {memory_state}: {format_bytes(int(memory['used_estimate_bytes']))} not easily reclaimable, {format_bytes(int(memory['reclaimable_bytes']))} reclaimable, and {format_bytes(int(memory['compressed_bytes']))} compressed.",
    ]

    if top_cpu_app and top_mem_app and top_cpu_app["family"] == top_mem_app["family"]:
        parts.append(
            f"{top_cpu_app['family']} is the main heavy hitter right now for both CPU and RAM."
        )
    else:
        if top_cpu_app:
            parts.append(
                f"Top CPU app: {top_cpu_app['family']} at {float(top_cpu_app['cpu_pct']):.1f}%."
            )
        if top_mem_app:
            parts.append(
                f"Top RAM app: {top_mem_app['family']} at {format_bytes(int(top_mem_app['rss_bytes']))}."
            )

    return " ".join(parts)


def collect_snapshot(limit: int, include_full_command: bool = False) -> dict[str, object]:
    if sys.platform != "darwin":
        raise RuntimeError("This script only supports macOS")

    sw_vers = parse_sw_vers(run_command("sw_vers"))
    total_bytes = int(run_command("sysctl", "-n", "hw.memsize").strip())
    logical_cpu_count = int(run_command("sysctl", "-n", "hw.logicalcpu").strip())
    vm = parse_vm_stat(run_command("vm_stat"))
    pressure = parse_memory_pressure(run_command("memory_pressure"))
    cpu = parse_top(run_command("top", "-l", "1", "-n", "0"))
    cpu["status"] = cpu_status(cpu)
    executable_names = parse_ucomm_map(run_command("ps", "-awwxo", "pid=,ucomm="))
    processes = parse_processes(
        run_command("ps", "-awwxo", "pid=,ppid=,user=,%cpu=,rss=,state=,command="),
        executable_names,
        include_full_command=include_full_command,
    )

    apps = aggregate_apps(processes)
    enrich_resource_shares(apps, total_bytes, logical_cpu_count)
    enrich_resource_shares(processes, total_bytes, logical_cpu_count)
    top_apps_by_cpu = sorted(
        apps, key=lambda item: float(item["cpu_pct"]), reverse=True
    )[:limit]
    top_apps_by_memory = sorted(
        apps, key=lambda item: int(item["rss_bytes"]), reverse=True
    )[:limit]
    top_processes_by_cpu = sorted(
        processes, key=lambda item: float(item["cpu_pct"]), reverse=True
    )[:limit]
    top_processes_by_memory = sorted(
        processes, key=lambda item: int(item["rss_bytes"]), reverse=True
    )[:limit]
    memory = memory_snapshot(total_bytes, vm, pressure)
    insights = build_insights(
        cpu, memory, top_apps_by_cpu, top_apps_by_memory, logical_cpu_count
    )

    return {
        "host": {
            "hostname": platform.node(),
            "product_name": sw_vers.get("ProductName"),
            "product_version": sw_vers.get("ProductVersion"),
            "build_version": sw_vers.get("BuildVersion"),
            "logical_cpu_count": logical_cpu_count,
            "total_memory_bytes": total_bytes,
        },
        "cpu": cpu,
        "memory": memory,
        "top_apps_by_cpu": top_apps_by_cpu,
        "top_apps_by_memory": top_apps_by_memory,
        "top_processes_by_cpu": top_processes_by_cpu,
        "top_processes_by_memory": top_processes_by_memory,
        "insights": insights,
        "plain_summary": plain_summary(
            cpu,
            memory,
            top_apps_by_cpu[0] if top_apps_by_cpu else None,
            top_apps_by_memory[0] if top_apps_by_memory else None,
        ),
        "notes": {
            "cpu_scale": "On macOS, 100% CPU is roughly one fully used logical core.",
            "memory_scale": "Grouped app totals are usually more useful than single helper processes, especially for browsers and Electron apps.",
            "privacy": "command_preview is redacted and truncated. Use --include-full-command only when you need raw argv and are comfortable exposing it.",
            "swap_counters": "swapins, swapouts, pageins, and pageouts are cumulative since boot, so use them as context rather than a current-rate alarm.",
        },
    }


def render_app_line(index: int, app: dict[str, object]) -> str:
    line = (
        f"{index}. {app['family']} - {float(app['cpu_pct']):.1f}% CPU, "
        f"{format_bytes(int(app['rss_bytes']))} RAM "
        f"({float(app.get('memory_share_pct', 0.0)):.1f}% of RAM) across {int(app['process_count'])} proc"
    )
    if app.get("note"):
        line += f". {app['note']}"
    return line


def render_process_line(index: int, process: dict[str, object]) -> str:
    line = (
        f"{index}. {process['family']} (pid {int(process['pid'])}) - {float(process['cpu_pct']):.1f}% CPU, "
        f"{format_bytes(int(process['rss_bytes']))} RAM "
        f"({float(process.get('memory_share_pct', 0.0)):.1f}% of RAM)"
    )
    if process.get("note"):
        line += f". {process['note']}"
    return line


def render_text(snapshot: dict[str, object]) -> str:
    host = dict(snapshot["host"])
    cpu = dict(snapshot["cpu"])
    memory = dict(snapshot["memory"])
    top_apps_by_cpu = list(snapshot["top_apps_by_cpu"])
    top_apps_by_memory = list(snapshot["top_apps_by_memory"])
    top_processes_by_cpu = list(snapshot["top_processes_by_cpu"])
    insights = list(snapshot["insights"])

    lines = [
        "Mac Resource Check",
        f"- Host: {host['product_name']} {host['product_version']} ({host['build_version']}), {int(host['logical_cpu_count'])} logical CPUs, {format_bytes(int(host['total_memory_bytes']))} RAM",
        f"- CPU: {float(cpu['busy_pct']):.1f}% busy ({float(cpu['user_pct']):.1f}% user, {float(cpu['system_pct']):.1f}% system, {float(cpu['idle_pct']):.1f}% idle)",
        f"- Memory: {format_bytes(int(memory['used_estimate_bytes']))} in use ({float(memory['used_estimate_pct']):.1f}%), {format_bytes(int(memory['reclaimable_bytes']))} reclaimable, {format_bytes(int(memory['compressed_bytes']))} compressed ({float(memory['compressed_pct']):.1f}%), {memory['memory_pressure_free_pct']}% free",
        f"- Summary: {snapshot['plain_summary']}",
        "",
        "Insights",
    ]

    for insight in insights:
        lines.append(
            f"- {insight['severity']}: {insight['title']} ({insight['id']})"
        )

    lines.extend(
        [
            "",
            "Top Apps By CPU",
        ]
    )

    for index, app in enumerate(top_apps_by_cpu, start=1):
        lines.append(render_app_line(index, app))

    lines.append("")
    lines.append("Top Apps By RAM")
    for index, app in enumerate(top_apps_by_memory, start=1):
        lines.append(render_app_line(index, app))

    lines.append("")
    lines.append("Top Individual Processes By CPU")
    for index, process in enumerate(top_processes_by_cpu, start=1):
        lines.append(render_process_line(index, process))

    lines.append("")
    lines.append(f"Note: {snapshot['notes']['cpu_scale']}")
    lines.append(f"Note: {snapshot['notes']['memory_scale']}")
    lines.append(f"Note: {snapshot['notes']['privacy']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect which apps and services are using CPU and RAM on macOS."
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON instead of plain text"
    )
    parser.add_argument(
        "--limit", type=int, default=6, help="How many top apps/processes to show"
    )
    parser.add_argument(
        "--include-full-command",
        action="store_true",
        help="Include unredacted full process command lines in JSON output",
    )
    args = parser.parse_args()

    snapshot = collect_snapshot(
        limit=max(1, args.limit), include_full_command=args.include_full_command
    )

    if args.json:
        print(json.dumps(snapshot, indent=2))
        return

    print(render_text(snapshot))


if __name__ == "__main__":
    main()
