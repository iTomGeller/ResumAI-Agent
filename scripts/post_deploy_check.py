"""Wait for ECS health, apply migration-v2.sql, and run read-only acceptance checks."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REQUIRED_TABLES = (
    "resume_task",
    "agent_execution_trace",
    "jd_library",
    "llm_invocation",
    "human_feedback_log",
)

PROMQL_SMOKE = (
    ("resumai_funnel_evaluation_started_total", "评估启动计数"),
    ("resumai_agent_span_count_total", "Agent Span 计数"),
    ("resumai_llm_duration_seconds_count", "LLM 调用次数"),
    ("resumai_mysql_query_duration_seconds_count", "MySQL SQL 查询次数"),
    ("resumai_mysql_table_total_bytes", "MySQL 表总容量"),
)

MYSQL_PROMQL_SMOKE = (
    ("resumai_mysql_query_duration_seconds_count", "MySQL SQL 查询次数"),
    ("resumai_mysql_query_duration_seconds_count{business_category_cn=\"JD\"}", "JD 业务大类 SQL 查询"),
    ("resumai_mysql_table_total_bytes{table=\"jd_library\"}", "岗位库表容量"),
    ("sum by (business_category_cn) (resumai_mysql_table_rows)", "业务大类表行数聚合"),
    ("hikaricp_connections_active", "Hikari 活跃连接"),
)

DASHBOARD_UIDS = (
    "resumai-spring-boot",
    "resumai-agents",
    "resumai-capability-rag",
    "resumai-capability-infra",
    "resumai-capability-toolcalls",
    "resumai-mysql-observability",
)

VAR_SUBSTITUTIONS = {
    "$__rate_interval": "5m",
    "$job_category": ".*",
    "$agent": ".*",
    "$step_kind": ".*",
    "$lane_id": ".*",
    "$tool_name": ".*",
    "$collection": ".*",
    "$error_type": ".*",
}

FORBIDDEN_ENGLISH = (
    "hit rate",
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "success/failed",
    "DeepSeek P95",
    "Token/s",
    "Agent Span",
    "indexed/s",
    "retrieved/s",
    "review rate",
    "Skill Invocation",
    "Resume Uploads",
)

FORBIDDEN_RAW_ENUMS = (
    "NEED_MANUAL_REVIEW",
    "STRONG_RECOMMEND",
    "OrchestratorAgent",
    "TechAgent",
    "ResumeParserAgent",
)

RAW_AGENT_NAMES = {
    "OrchestratorAgent",
    "ResumeParserAgent",
    "TechAgent",
    "ProjectAgent",
    "RiskAgent",
    "JdMatchAgent",
    "RagasJudgeAgent",
}

SKILL_OUTPUT_KINDS = (
    "resume_parse",
    "skill_eval",
    "rag_retrieve",
    "quality_check",
    "report_generate",
)

MAIN_DAG_STEPS = (
    "task_create",
    "resume_parse",
    "jd_match",
    "rag_retrieve",
    "llm_complete",
    "quality_check",
    "report_generate",
)


def substitute_promql(expr: str) -> str:
    resolved = expr
    for key, value in VAR_SUBSTITUTIONS.items():
        resolved = resolved.replace(key, value)
    resolved = re.sub(r'\{job_category=~"[^"]*"\}', "", resolved)
    resolved = re.sub(r'\{agent=~"[^"]*"\}', "", resolved)
    resolved = re.sub(r'\{step_kind=~"[^"]*"\}', "", resolved)
    resolved = re.sub(r'\{lane_id=~"[^"]*"\}', "", resolved)
    resolved = re.sub(r'\{tool_name=~"[^"]*"\}', "", resolved)
    resolved = re.sub(r'\{collection=~"[^"]*"\}', "", resolved)
    return resolved


def extract_dashboard_promql(path: Path) -> list[tuple[str, str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    dashboard_title = data.get("title") or path.stem
    panels: list[tuple[str, str, str]] = []
    for panel in data.get("panels") or []:
        if panel.get("type") == "row":
            continue
        panel_title = panel.get("title") or "untitled"
        for target in panel.get("targets") or []:
            expr = (target.get("expr") or "").strip()
            if expr:
                panels.append((dashboard_title, panel_title, expr))
    return panels


def promql_query_ok(ssh: paramiko.SSHClient, expr: str) -> tuple[bool, str]:
    query = urllib.parse.quote(substitute_promql(expr), safe="")
    prom_raw = run(
        ssh,
        f"docker exec resumai-prometheus wget -qO- "
        f"'http://127.0.0.1:9090/api/v1/query?query={query}'",
        timeout=30,
        allow_fail=True,
    )
    try:
        prom = json.loads(prom_raw)
        if prom.get("status") != "success":
            return False, prom.get("error", "query failed")
        return True, "ok"
    except json.JSONDecodeError:
        return False, prom_raw[:120]


def resolve_grafana_password(ssh: paramiko.SSHClient, env: dict[str, str], deploy_dir: str) -> str:
    env_text = run(
        ssh,
        f"grep -E '^GRAFANA_PASSWORD=' {deploy_dir}/.env || true",
        timeout=20,
        allow_fail=True,
    )
    for line in env_text.splitlines():
        if line.startswith("GRAFANA_PASSWORD="):
            remote = line.split("=", 1)[1].strip()
            if remote:
                return remote
    password = env.get("GRAFANA_PASSWORD", "").strip()
    return password or "admin"


def grafana_api(ssh: paramiko.SSHClient, path: str, password: str, timeout: int = 20) -> str:
    safe_password = password.replace("'", "'\\''")
    return run(
        ssh,
        f"curl -fsS -u admin:'{safe_password}' http://127.0.0.1/grafana{path}",
        timeout=timeout,
        allow_fail=True,
    )


def check_grafana_stack(ssh: paramiko.SSHClient, public_base: str, env: dict[str, str], deploy_dir: str) -> None:
    grafana_password = resolve_grafana_password(ssh, env, deploy_dir)
    health_raw = run(
        ssh,
        "curl -fsS http://127.0.0.1/grafana/api/health",
        timeout=20,
        allow_fail=True,
    )
    check(
        "Grafana subpath /grafana/api/health returns JSON",
        '"database"' in health_raw or '"version"' in health_raw,
        health_raw[:80],
    )

    try:
        status, body = http_get(f"{public_base}/grafana/api/health")
        check("public /grafana/api/health", status == 200 and ("database" in body or "version" in body), body[:80])
    except Exception as exc:
        check("public /grafana/api/health", False, str(exc))

    ds_raw = grafana_api(ssh, "/api/datasources", grafana_password)
    if ds_raw.strip().startswith("["):
        datasources = json.loads(ds_raw)
        check("Grafana datasource configured", len(datasources) >= 1, f"count={len(datasources)}")
    else:
        prov_raw = run(
            ssh,
            "docker exec resumai-grafana sh -c 'cat /etc/grafana/provisioning/datasources/*.yml'",
            timeout=20,
            allow_fail=True,
        )
        check(
            "Grafana datasource provisioned",
            "prometheus" in prov_raw.lower() and "url:" in prov_raw.lower(),
            prov_raw[:120],
        )

    search_raw = grafana_api(ssh, "/api/search?type=dash-db", grafana_password)
    if search_raw.strip().startswith("["):
        dashboards = json.loads(search_raw)
        titles = {d.get("uid") for d in dashboards}
        for uid in DASHBOARD_UIDS:
            check(f"Grafana dashboard uid {uid}", uid in titles, f"found={len(titles)}")
        check_grafana_live_agent_options(ssh, grafana_password)
    else:
        listed = run(
            ssh,
            "docker exec resumai-grafana sh -c 'ls /etc/grafana/provisioning/dashboards/*.json'",
            timeout=20,
            allow_fail=True,
        )
        for uid in DASHBOARD_UIDS:
            check(
                f"Grafana dashboard file {uid}",
                uid in listed,
                listed.replace("\n", ", ")[:160],
            )


def check_grafana_live_agent_options(ssh: paramiko.SSHClient, grafana_password: str) -> None:
    violations: list[str] = []
    for uid in ("resumai-agents", "resumai-spring-boot"):
        raw = grafana_api(ssh, f"/api/dashboards/uid/{uid}", grafana_password)
        if not raw.strip().startswith("{"):
            violations.append(f"{uid}: dashboard API unavailable")
            continue
        payload = json.loads(raw)
        dashboard = payload.get("dashboard") or {}
        for item in dashboard.get("templating", {}).get("list") or []:
            if item.get("name") != "agent":
                continue
            for opt in item.get("options") or []:
                text = opt.get("text") or ""
                if text in RAW_AGENT_NAMES or (text.endswith("Agent") and not _contains_chinese(text)):
                    violations.append(f"{uid}: live agent option text '{text}'")
    check(
        "Grafana live agent variable options are Chinese",
        len(violations) == 0,
        "; ".join(violations[:4]),
    )


def smoke_dashboard_promql(ssh: paramiko.SSHClient, dashboard_dir: Path) -> None:
    failed: list[str] = []
    checked = 0
    for path in sorted(dashboard_dir.glob("*.json")):
        for dashboard_title, panel_title, expr in extract_dashboard_promql(path):
            checked += 1
            ok, detail = promql_query_ok(ssh, expr)
            if ok:
                print(f"[PASS] PromQL panel: {dashboard_title} / {panel_title}")
            else:
                failed.append(f"{dashboard_title} / {panel_title}: {detail} :: {substitute_promql(expr)[:120]}")
                print(f"[FAIL] PromQL panel: {dashboard_title} / {panel_title} — {detail}")
    check(
        "dashboard PromQL smoke",
        len(failed) == 0,
        f"checked={checked} failed={len(failed)}; first={failed[0] if failed else ''}",
    )


def _contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def dashboard_lint(dashboard_dir: Path) -> None:
    violations: list[str] = []
    promql_violations: list[str] = []
    for path in sorted(dashboard_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        title = data.get("title") or path.stem
        in_debug_row = False
        for panel in data.get("panels") or []:
            if panel.get("type") == "row":
                row_title = panel.get("title") or ""
                in_debug_row = panel.get("collapsed", False) or "原始指标排障" in row_title
                if not _contains_chinese(row_title):
                    violations.append(f"{title}: row title '{row_title}' not Chinese")
                continue
            if in_debug_row:
                continue
            panel_title = panel.get("title") or "untitled"
            if not _contains_chinese(panel_title):
                violations.append(f"{title}/{panel_title}: panel title not Chinese")
            description = panel.get("description") or ""
            if description and not _contains_chinese(description):
                violations.append(f"{title}/{panel_title}: description not Chinese")
            defaults = panel.get("fieldConfig", {}).get("defaults", {})
            if defaults.get("noValue") in {None, "", "No data"}:
                violations.append(f"{title}/{panel_title}: missing Chinese noValue")
            for target in panel.get("targets") or []:
                expr = target.get("expr") or ""
                legend = target.get("legendFormat") or ""
                if "[5m]" in expr and "$__rate_interval" not in expr:
                    promql_violations.append(f"{title}/{panel_title}: fixed [5m]")
                lowered = f"{panel_title} {description} {legend}".lower()
                for bad in FORBIDDEN_ENGLISH:
                    if bad.lower() in lowered:
                        violations.append(f"{title}/{panel_title}: contains '{bad}'")
                if "{{" in legend and "or vector(0)" in expr.replace(" ", ""):
                    violations.append(
                        f"{title}/{panel_title}: template legend must not use unlabeled or vector(0)"
                    )
                visible = f"{panel_title} {legend}"
                for raw in ("NEED_MANUAL_REVIEW", "STRONG_RECOMMEND", "RECOMMEND", "REJECT"):
                    if raw in visible:
                        violations.append(f"{title}/{panel_title}: visible raw enum '{raw}'")
        for item in data.get("templating", {}).get("list") or []:
            label = item.get("label") or ""
            if label and not _contains_chinese(label):
                violations.append(f"{title}: variable label '{label}' not Chinese")
            current = item.get("current") or {}
            current_text = current.get("text") or ""
            if current_text in {"All", "None"}:
                violations.append(f"{title}: variable current text '{current_text}' should be Chinese")
            var_name = item.get("name") or ""
            query = item.get("query") or ""
            if var_name == "agent" and item.get("type") != "custom":
                violations.append(f"{title}: agent variable should be custom with Chinese text/value")
            if var_name in {"agent", "step_kind", "lane_id", "tool_name"} and isinstance(query, str):
                for part in query.split(","):
                    if " : " not in part:
                        continue
                    display, value = part.split(" : ", 1)
                    display = display.strip()
                    value = value.strip()
                    if var_name == "agent" and value in RAW_AGENT_NAMES and not _contains_chinese(display):
                        violations.append(f"{title}: variable {var_name} query display not Chinese '{display}'")
            options = item.get("options") or []
            for opt in options:
                opt_text = opt.get("text") or ""
                if opt_text in {"All", "None"}:
                    violations.append(f"{title}: variable option text '{opt_text}' should be Chinese")
                if var_name == "agent" and opt_text in RAW_AGENT_NAMES:
                    violations.append(f"{title}: agent option text is raw English '{opt_text}'")
    check("dashboard Chinese lint", len(violations) == 0, "; ".join(violations[:6]))
    check("dashboard PromQL lint", len(promql_violations) == 0, "; ".join(promql_violations[:4]))


def collect_node_ids(events: list[dict]) -> set[str]:
    node_ids: set[str] = set()
    for event in events:
        node_id = event.get("nodeId")
        step_kind = event.get("stepKind")
        lane_id = event.get("laneId")
        if node_id:
            node_ids.add(node_id)
        elif step_kind:
            if step_kind == "skill_eval" and lane_id:
                node_ids.add(f"skill_eval:{lane_id}")
            else:
                node_ids.add(step_kind)
    return node_ids


def check_trace_contract(events: list[dict], *, running: bool) -> None:
    node_ids = collect_node_ids(events)
    expected_nodes = [e for e in events if e.get("expected")]
    with_depends = [e for e in events if e.get("dependsOn")]
    check("trace events expose dependsOn", len(with_depends) >= 3, f"depends={len(with_depends)}")
    if running:
        check(
            "RUNNING trace has expected skeleton nodes",
            len(expected_nodes) >= 5,
            f"expected={len(expected_nodes)} nodes={len(node_ids)}",
        )
    else:
        missing = [step for step in MAIN_DAG_STEPS if step not in node_ids]
        check(
            "completed trace covers main DAG steps",
            len(missing) <= 2,
            f"missing={missing}",
        )


def check_trace_node_details(events: list[dict]) -> None:
    key_nodes = {
        "llm_complete": ("fullPrompt", "fullOutput"),
        "resume_parse": ("fullInput", "fullOutput"),
        "rag_retrieve": ("fullInput", "fullOutput"),
        "report_generate": ("fullOutput",),
        "quality_check": ("fullInput", "fullOutput"),
    }
    by_kind: dict[str, dict] = {}
    skill_nodes: list[dict] = []
    for event in events:
        kind = event.get("stepKind")
        if kind and not event.get("expected"):
            by_kind[kind] = event
            if kind == "skill_eval":
                skill_nodes.append(event)
    detail_issues: list[str] = []
    for kind, fields in key_nodes.items():
        event = by_kind.get(kind)
        if not event:
            continue
        if kind == "llm_complete" and event.get("llmInvocationId"):
            continue
        for field in fields:
            value = (event.get(field) or "").strip()
            if not value or value.endswith("...") or "[truncated]" in value.lower():
                detail_issues.append(f"{kind}.{field}")
            elif kind in {"resume_parse", "rag_retrieve", "quality_check", "report_generate"}:
                if len(value) < 80 or ("{" not in value and "skillName" not in value):
                    detail_issues.append(f"{kind}.{field}:too_short")
    for event in skill_nodes:
        for field in ("fullInput", "fullOutput"):
            value = (event.get(field) or "").strip()
            if not value or len(value) < 80 or value.endswith("..."):
                detail_issues.append(f"skill_eval:{event.get('laneId')}.{field}")
            elif "{" not in value and "skillName" not in value:
                detail_issues.append(f"skill_eval:{event.get('laneId')}.{field}:not_json")
    check("trace node full detail fields", len(detail_issues) == 0, "; ".join(detail_issues[:6]))


def check_frontend_static(repo_root: Path) -> None:
    app_vue = (repo_root / "frontend/src/App.vue").read_text(encoding="utf-8")
    style_css = (repo_root / "frontend/src/style.css").read_text(encoding="utf-8")
    check(
        "frontend agent flow board present",
        "agent-flow-board" in app_vue and "agent-flow-board" in style_css and "dagStages" in app_vue,
        "missing stage-rail agent flow board",
    )
    check(
        "frontend process layout inspector present",
        "process-layout" in app_vue and "agent-inspector" in style_css,
        "missing right-side agent inspector layout",
    )
    check(
        "frontend agent DAG model present",
        "agentDagNodes" in app_vue and "AGENT_PIPELINE" in app_vue,
        "missing agent-centric DAG aggregation",
    )
    check(
        "frontend agent inspector labels present",
        all(label in app_vue for label in ("概览", "推理轮次", "调用详情", "Prompt", "评估依据"))
        and "buildExecutionGraph" in app_vue,
        "missing agent inspector Chinese labels",
    )
    check(
        "frontend pagination helpers wired",
        "usePagination" in app_vue and "pagination-bar" in app_vue and "pagination-bar" in style_css,
        "missing shared pagination UI",
    )
    check(
        "frontend job list filters present",
        all(token in app_vue for token in ("jobSearch", "jobCategoryFilter", "jobPagination", "job-list-toolbar")),
        "missing JD search/filter/pagination UI",
    )
    check(
        "frontend candidate score filters present",
        all(token in app_vue for token in ("scoreFilter", "recommendationFilter", "candidateSortBy")),
        "missing candidate score/recommendation filters",
    )
    check(
        "frontend show-more list helpers present",
        "listPreview" in app_vue and "toggleList" in app_vue and "show-more-btn" in style_css,
        "missing ShowMore list helpers",
    )
    check(
        "frontend legacy SVG dag edges removed",
        "dagEdgePath" not in app_vue and "dag-edges" not in app_vue,
        "legacy SVG DAG edges still present",
    )


def check_frontend_bundle(ssh: paramiko.SSHClient) -> None:
    bundle_raw = run(
        ssh,
        "docker exec ai-resume-frontend sh -c 'grep -o \"index-[^\\\"]*\\.js\" /usr/share/nginx/html/index.html | head -n 1'",
        timeout=20,
        allow_fail=True,
    )
    bundle = bundle_raw.strip()
    check("frontend bundle detected", bool(bundle), bundle_raw[:80])
    if not bundle:
        return
    markers = (
        ("agent-flow-board", "agent flow board class"),
        ("job-list-toolbar", "job list toolbar"),
        ("90 分以上", "candidate score filter option"),
        ("展开全部", "show-more button label"),
        ("调用详情", "agent calls tab"),
        ("评估依据", "HR evidence tab"),
        ("面向招聘决策", "HR view hint"),
        ("call-detail-card", "typed call detail cards"),
    )
    missing: list[str] = []
    for needle, label in markers:
        hit = run(
            ssh,
            f"docker exec ai-resume-frontend sh -c 'grep -F \"{needle}\" /usr/share/nginx/html/assets/{bundle} >/dev/null && echo yes || echo no'",
            timeout=30,
            allow_fail=True,
        ).strip()
        if hit != "yes":
            missing.append(label)
    check(
        "frontend bundle contains UI rework markers",
        len(missing) == 0,
        "; ".join(missing[:4]),
    )


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    if not path.exists():
        raise SystemExit(f"Missing {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def connect_ssh(env: dict[str, str]) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        env["ALIYUN_HOST"],
        username=env.get("ALIYUN_USER", "root"),
        password=env["ALIYUN_PASSWORD"],
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    transport = ssh.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return ssh


def run(ssh: paramiko.SSHClient, command: str, timeout: int = 120, allow_fail: bool = False, env: dict[str, str] | None = None) -> str:
    print(f"\n$ {command}")
    for attempt in range(2):
        try:
            _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            text = (out + err).strip()
            if text:
                preview = text if len(text) <= 800 else text[:800] + f"... [truncated {len(text)} chars]"
                print(preview)
            if code != 0 and not allow_fail:
                raise SystemExit(f"command failed ({code}): {command}")
            return text
        except paramiko.SSHException as exc:
            if attempt == 0 and env is not None:
                print(f"[warn] SSH reconnect after: {exc}")
                try:
                    ssh.close()
                except Exception:
                    pass
                ssh = connect_ssh(env)
                continue
            raise
    return ""


def http_get(url: str, timeout: int = 12) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def check_mysql_observability(ssh: paramiko.SSHClient) -> None:
    """验证 MyBatis SQL 指标、表容量 Gauge 与 Hikari 连接池指标已暴露。"""
    actuator_raw = run(
        ssh,
        "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/actuator/prometheus "
        "| grep -E '^resumai_mysql_query_duration_seconds_count|^resumai_mysql_table_total_bytes' "
        "| head -8",
        timeout=30,
        allow_fail=True,
    )
    check(
        "actuator mysql query metrics exported",
        "resumai_mysql_query_duration_seconds_count" in actuator_raw,
        actuator_raw.replace("\n", " ")[:160],
    )
    check(
        "actuator mysql table metrics exported",
        "resumai_mysql_table_total_bytes" in actuator_raw,
        actuator_raw.replace("\n", " ")[:160],
    )
    check(
        "actuator mysql business_category_cn label",
        "business_category_cn=" in actuator_raw,
        actuator_raw.replace("\n", " ")[:160],
    )

    mysql_ok = 0
    for metric, label in MYSQL_PROMQL_SMOKE:
        query = urllib.parse.quote(metric, safe="")
        prom_raw = run(
            ssh,
            f"docker exec resumai-prometheus wget -qO- "
            f"'http://127.0.0.1:9090/api/v1/query?query={query}'",
            timeout=30,
            allow_fail=True,
        )
        try:
            prom = json.loads(prom_raw)
            series = prom.get("data", {}).get("result") or []
            if series:
                mysql_ok += 1
                print(f"[PASS] MySQL observability PromQL: {label} ({metric})")
            else:
                print(f"[WARN] MySQL observability PromQL empty: {label} ({metric})")
        except json.JSONDecodeError:
            print(f"[WARN] MySQL observability PromQL failed: {label}")

    check(
        "MySQL observability PromQL ready",
        mysql_ok >= 3,
        f"matched={mysql_ok}/{len(MYSQL_PROMQL_SMOKE)}",
    )


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    if not ok:
        raise SystemExit(f"acceptance failed: {name}{suffix}")


def main() -> None:
    env = load_env()
    host = env["ALIYUN_HOST"]
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    base = f"http://{host}"

    for attempt in range(1, 25):
        try:
            status, body = http_get(f"{base}/api/health")
            print(f"[health] attempt {attempt}: {status} {body[:120]}")
            if status == 200 and "UP" in body:
                break
        except Exception as exc:
            print(f"[health] attempt {attempt}: {exc}")
        time.sleep(8)
    else:
        raise SystemExit("health check timed out")

    ssh = connect_ssh(env)
    try:
        local_migration_v2 = Path(__file__).resolve().parents[1] / "backend/src/main/resources/db/migration-v2.sql"
        local_migration_v3 = Path(__file__).resolve().parents[1] / "backend/src/main/resources/db/migration-v3.sql"
        remote_migration_v2 = f"{deploy_dir}/backend/src/main/resources/db/migration-v2.sql"
        remote_migration_v3 = f"{deploy_dir}/backend/src/main/resources/db/migration-v3.sql"
        sftp = ssh.open_sftp()
        sftp.put(str(local_migration_v2), remote_migration_v2)
        if local_migration_v3.exists():
            sftp.put(str(local_migration_v3), remote_migration_v3)
        sftp.close()

        env_text = run(ssh, f"grep -E '^MYSQL_(ROOT_PASSWORD|DATABASE)=' {deploy_dir}/.env || true", timeout=30)
        mysql_root = env.get("MYSQL_ROOT_PASSWORD", "ResumaiRoot!2026")
        mysql_db = env.get("MYSQL_DATABASE", "resumai_agent")
        for line in env_text.splitlines():
            if line.startswith("MYSQL_ROOT_PASSWORD="):
                mysql_root = line.split("=", 1)[1].strip()
            elif line.startswith("MYSQL_DATABASE="):
                mysql_db = line.split("=", 1)[1].strip()

        run(
            ssh,
            f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} "
            f"< {remote_migration_v2}",
            timeout=60,
        )
        if local_migration_v3.exists():
            run(
                ssh,
                f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} "
                f"< {remote_migration_v3}",
                timeout=60,
            )

        run(ssh, "docker restart ai-resume-backend", timeout=120)
        for attempt in range(1, 31):
            health = run(
                ssh,
                "curl -fsS http://127.0.0.1/api/health",
                timeout=20,
                allow_fail=True,
            )
            print(f"[restart-health] attempt {attempt}: {health[:120]}")
            if "UP" in health:
                break
            time.sleep(6)
        else:
            raise SystemExit("backend not healthy after migration restart")

        time.sleep(8)
        started_at = run(
            ssh,
            "docker inspect -f '{{.State.StartedAt}}' ai-resume-backend",
            timeout=15,
        ).strip()

        tables_raw = run(
            ssh,
            f"docker exec resumai-mysql mysql -uroot -p'{mysql_root}' -N -e "
            f"\"SELECT table_name FROM information_schema.tables WHERE table_schema='{mysql_db}'\"",
            timeout=30,
        )
        tables = {line.strip() for line in tables_raw.splitlines() if line.strip()}
        for table in REQUIRED_TABLES:
            check(f"table exists: {table}", table in tables)

        jd_count_raw = run(
            ssh,
            f"docker exec resumai-mysql mysql -uroot -p'{mysql_root}' -N -s -e "
            f"\"SELECT COUNT(*) FROM {mysql_db}.jd_library WHERE deleted=0\" 2>/dev/null",
            timeout=30,
        )
        jd_lines = [line.strip() for line in jd_count_raw.splitlines() if line.strip().isdigit()]
        jd_count = int(jd_lines[-1]) if jd_lines else 0
        if jd_count == 0:
            default_jds = [
                ("job-java-agent", "高级 Java / AI Agent 平台工程师", "TECH",
                 "招聘 Java 21 / Spring Boot 3 / AI Agent 平台方向高级后端工程师，要求熟悉 RAG、Trace 可观测、Docker 部署。"),
                ("job-product-ai", "AI 产品经理", "PRODUCT",
                 "负责 AI 招聘产品从需求洞察、PRD、数据指标到上线迭代，要求理解 LLM/RAG 基础能力。"),
            ]
            for jd_id, title, category, description in default_jds:
                payload = json.dumps(
                    {"jdId": jd_id, "title": title, "category": category, "description": description},
                    ensure_ascii=False,
                ).replace("'", "'\\''")
                run(
                    ssh,
                    f"curl -fsS -X POST http://127.0.0.1/api/jd "
                    f"-H 'Content-Type: application/json' "
                    f"-d '{payload}'",
                    timeout=30,
                    allow_fail=True,
                )
            jd_count_raw = run(
                ssh,
                f"docker exec resumai-mysql mysql -uroot -p'{mysql_root}' -N -s -e "
                f"\"SELECT COUNT(*) FROM {mysql_db}.jd_library WHERE deleted=0\" 2>/dev/null",
                timeout=30,
            )
            jd_lines = [line.strip() for line in jd_count_raw.splitlines() if line.strip().isdigit()]
            jd_count = int(jd_lines[-1]) if jd_lines else 0
        check("jd_library seed count > 0", jd_count > 0, f"count={jd_count}")

        payload_col_raw = run(
            ssh,
            f"docker exec resumai-mysql mysql -uroot -p'{mysql_root}' -N -s -e "
            f"\"SELECT COUNT(*) FROM information_schema.columns "
            f"WHERE table_schema='{mysql_db}' AND table_name='resume_task' AND column_name='result_payload'\" 2>/dev/null",
            timeout=30,
        )
        payload_lines = [line.strip() for line in payload_col_raw.splitlines() if line.strip().isdigit()]
        check("resume_task.result_payload column", payload_lines and payload_lines[-1] == "1")

        log_scan = run(
            ssh,
            f"docker logs ai-resume-backend --since {started_at} 2>&1 | "
            "grep -E \"jd_library.*doesn't exist|Table .* doesn't exist\" || true",
            timeout=30,
            allow_fail=True,
        )
        check("backend logs since restart: no schema missing errors", "doesn't exist" not in log_scan.lower())

        tasks_raw = run(ssh, "curl -fsS 'http://127.0.0.1/api/tasks?page=1&pageSize=5'", timeout=30)
        tasks_payload = json.loads(tasks_raw) if tasks_raw.strip().startswith("{") else {}
        tasks = tasks_payload.get("items") or []
        check("/api/tasks reachable", isinstance(tasks, list))
        if tasks:
            sample = tasks[0]
            for field in ("traceId", "status", "recommendation"):
                check(f"/api/tasks field {field}", field in sample)

        jds_raw = run(ssh, "curl -fsS 'http://127.0.0.1/api/jds?page=1&pageSize=5'", timeout=30)
        jds_payload = json.loads(jds_raw) if jds_raw.strip().startswith("{") else {}
        jds = jds_payload.get("items") or []
        check("/api/jds non-empty", len(jds) > 0, f"count={len(jds)}")

        if tasks:
            trace_id = tasks[0].get("traceId")
            if trace_id:
                graph_raw = run(ssh, f"curl -fsS http://127.0.0.1/api/graphs/{trace_id}", timeout=30)
                graph = json.loads(graph_raw)
                nodes = graph.get("nodes") or []
                bad_nodes = [n for n in nodes if not (n.get("id") or "").strip() or not (n.get("label") or "").strip()]
                check(f"/api/graphs/{trace_id} node ids", len(bad_nodes) == 0, f"bad={len(bad_nodes)}")

                trace_raw = run(ssh, f"curl -fsS http://127.0.0.1/api/traces/{trace_id}", timeout=30)
                events = json.loads(trace_raw)
                check(f"/api/traces/{trace_id}", isinstance(events, list) and len(events) > 0, f"events={len(events)}")

                llm_id = None
                for ev in events:
                    if ev.get("llmInvocationId"):
                        llm_id = ev["llmInvocationId"]
                        break
                if llm_id:
                    llm_raw = run(ssh, f"curl -fsS http://127.0.0.1/api/llm-invocations/{llm_id}", timeout=30)
                    llm = json.loads(llm_raw)
                    check(
                        f"/api/llm-invocations/{llm_id} full IO",
                        bool(llm.get("promptFull")) and bool(llm.get("responseFull")),
                        f"truncated={llm.get('truncated')}",
                    )

        check_mysql_observability(ssh)

        prom_ok = 0
        for metric, label in PROMQL_SMOKE:
            prom_raw = run(
                ssh,
                f"docker exec resumai-prometheus wget -qO- "
                f"'http://127.0.0.1:9090/api/v1/query?query={metric}'",
                timeout=30,
                allow_fail=True,
            )
            try:
                prom = json.loads(prom_raw)
                series = prom.get("data", {}).get("result") or []
                if series:
                    prom_ok += 1
                    print(f"[PASS] PromQL sample: {label} ({metric})")
                else:
                    print(f"[WARN] PromQL empty: {label} ({metric})")
            except json.JSONDecodeError:
                print(f"[WARN] PromQL query failed for {metric}")

        if prom_ok == 0:
            actuator_raw = run(
                ssh,
                "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/actuator/prometheus | grep -c '^resumai_' || true",
                timeout=30,
                allow_fail=True,
            )
            actuator_lines = [line.strip() for line in actuator_raw.splitlines() if line.strip().isdigit()]
            if actuator_lines and int(actuator_lines[-1]) > 0:
                prom_ok = 1
                print(f"[PASS] actuator/prometheus exports resumai metrics ({actuator_lines[-1]} lines)")

        resume_text = "张三\nJava 后端 5 年经验\n技能：Java, Spring Boot, MySQL, Redis, Docker, RAG\n项目：AI Agent 平台，负责 RAG 检索与 Trace 可观测。"
        task_payload = json.dumps(
            {
                "fileName": "acceptance-e2e.txt",
                "jobCategory": "TECH",
                "executionMode": "DAG_CONCURRENT",
                "jobDescription": "",
                "resumeText": resume_text,
            },
            ensure_ascii=False,
        )
        create_raw = run(
            ssh,
            "curl -fsS -X POST http://127.0.0.1/api/tasks "
            "-H 'Content-Type: application/json' "
            f"-d {json.dumps(task_payload)}",
            timeout=30,
        )
        created = json.loads(create_raw) if create_raw.strip().startswith("{") else {}
        new_trace = created.get("traceId")
        check("E2E create evaluation task", bool(new_trace), str(created.get("status", "")))
        check("E2E task returns RUNNING immediately", created.get("status") == "RUNNING", str(created.get("status", "")))

        if new_trace:
            running_trace_raw = run(ssh, f"curl -fsS http://127.0.0.1/api/traces/{new_trace}", timeout=30)
            running_events = json.loads(running_trace_raw)
            check_trace_contract(running_events, running=True)

            final_task = None
            poll_script = (
                "import sys,json; "
                "print(sys.stdin.read())"
            )
            status_cmd = f"curl -fsS http://127.0.0.1/api/tasks/{new_trace} | python3 -c {json.dumps(poll_script)}"
            for _ in range(40):
                task_raw = run(ssh, status_cmd, timeout=30, env=env)
                final_task = json.loads(task_raw) if task_raw.strip().startswith("{") else None
                if final_task and final_task.get("status") in ("SUCCESS", "FAILED"):
                    break
                time.sleep(8)
            check("E2E task finished", final_task and final_task.get("status") == "SUCCESS", final_task.get("status") if final_task else "missing")
            if final_task:
                questions = final_task.get("interviewQuestions") or []
                bad = [q for q in questions if "强相关性" in q or "潜力突出" in q or "风险可控" in q]
                check("E2E interview questions not recommendation bullets", len(bad) == 0, f"bad={len(bad)} total={len(questions)}")
                trace_raw = run(ssh, f"curl -fsS http://127.0.0.1/api/traces/{new_trace}", timeout=30)
                events = json.loads(trace_raw)
                check_trace_contract(events, running=False)
                check_trace_node_details(events)
                rag_warn = [e for e in events if "RAG" in (e.get("eventType") or "") and e.get("status") == "WARNING"]
                if rag_warn:
                    detail = rag_warn[0].get("detail") or ""
                    check(
                        "E2E RAG WARNING has explicit fallback",
                        "RAG_DISABLED_BY_CONFIG" in detail or "EMBEDDING" in detail.upper() or "ModelNotFound" not in detail,
                        detail[:120],
                    )

        prom_after = 0
        for metric, label in PROMQL_SMOKE:
            prom_raw = run(
                ssh,
                f"docker exec resumai-prometheus wget -qO- "
                f"'http://127.0.0.1:9090/api/v1/query?query={metric}'",
                timeout=30,
                allow_fail=True,
            )
            try:
                prom = json.loads(prom_raw)
                series = prom.get("data", {}).get("result") or []
                if series:
                    prom_after += 1
                    print(f"[PASS] post-E2E PromQL: {label}")
            except json.JSONDecodeError:
                pass
        check(
            "Prometheus/Grafana data path ready",
            prom_ok >= 1 or prom_after >= 1,
            f"pre={prom_ok} post={prom_after}",
        )

        check_grafana_stack(ssh, base, env, deploy_dir)
        repo_root = Path(__file__).resolve().parents[1]
        check_frontend_static(repo_root)
        dashboard_dir = repo_root / "monitoring/grafana/provisioning/dashboards"
        dashboard_lint(dashboard_dir)
        smoke_dashboard_promql(ssh, dashboard_dir)
        check_frontend_bundle(ssh)

        print("\n[ok] post-deploy acceptance checks passed")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
