import json

CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
UNAVAILABLE = "unavailable"

LABELS = {
    CONFIRMED: "навык подтверждён правилом продвижения",
    UNCONFIRMED: "нет подтверждённого навыка — ориентировочная оценка, не предписание",
    UNAVAILABLE: "нет артефакта навыка — рекомендации по переменной не выдаются",
}
DIRECTIVES = {CONFIRMED: "Запланировать", UNCONFIRMED: "Ориентировочно (не предписание)", UNAVAILABLE: ""}


def combo_status(skill, variable, mode, lead=None):
    combos = skill.get("combos") or {}
    if not combos:
        return UNCONFIRMED
    keys = [k for k in combos if k.startswith(f"{mode}_{variable}_l")]
    if not keys:
        return UNAVAILABLE
    if lead is not None and f"{mode}_{variable}_l{int(lead)}" in combos:
        keys = [f"{mode}_{variable}_l{int(lead)}"]
    else:
        keys = sorted(keys, key=lambda k: int(k.rsplit("_l", 1)[1]))[:1]
    c = combos[keys[0]]
    return CONFIRMED if c.get("skill_promoted") else UNCONFIRMED


def skill_path(config, region="krai"):
    from pathlib import Path

    name = f"{region}_grid_skill.json"
    if config is None:
        return None
    for base in (getattr(config, "bundle_dir", None), getattr(config, "artifact_dir", None)):
        if not base:
            continue
        for cand in (Path(base) / "artifacts" / name, Path(base) / name):
            if cand.exists():
                return cand
    for base in (getattr(config, "bundle_dir", None), getattr(config, "artifact_dir", None)):
        if not base:
            continue
        for art in (Path(base) / "artifacts", Path(base)):
            if art.is_dir():
                found = sorted(art.glob("*_grid_skill.json"))
                if len(found) == 1:
                    return found[0]
    return None


def load_evidence(config, mode="seasonal", region="krai"):
    out = {}
    p = skill_path(config, region)
    if p is None or not p.exists():
        for v in ("t2m", "tp"):
            out[v] = {"status": UNAVAILABLE, "label": LABELS[UNAVAILABLE], "source": "skill artifact missing"}
        return out
    try:
        skill = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        for v in ("t2m", "tp"):
            out[v] = {"status": UNAVAILABLE, "label": LABELS[UNAVAILABLE], "source": f"skill artifact unreadable: {type(exc).__name__}"}
        return out
    for v in ("t2m", "tp"):
        st = combo_status(skill, v, mode)
        out[v] = {"status": st, "label": LABELS[st], "source": str(skill.get("schema") or "legacy"),
                  "years": skill.get("years"), "generated_at": skill.get("generated_at")}
    return out


def tag_agro(agro, evidence):
    if not agro:
        return agro
    agro["policy"] = {"evidence": {v: d["status"] for v, d in evidence.items()},
                      "detail": evidence,
                      "rule": "предписания выдаются только при подтверждённом навыке (T11/T12)"}
    ins = agro.get("insight") or {}
    water = ins.get("water") or {}
    if water:
        water["policy"] = evidence.get("tp", {"status": UNAVAILABLE, "label": LABELS[UNAVAILABLE]})
    drought = ins.get("drought") or {}
    if drought:
        drought["policy"] = evidence.get("tp", {"status": UNAVAILABLE, "label": LABELS[UNAVAILABLE]})
        if drought.get("irrigation_hint_m3_ha") is not None and drought["policy"]["status"] == UNAVAILABLE:
            drought.pop("irrigation_hint_m3_ha", None)
    frost = ins.get("frost") or {}
    if frost.get("crop"):
        frost["crop"]["policy"] = evidence.get("t2m", {"status": UNAVAILABLE, "label": LABELS[UNAVAILABLE]})
    for c in agro.get("decisions") or []:
        src = "t2m" if c.get("key") == "antistress" else "tp"
        c["evidence_variable"] = src
        c["policy"] = evidence.get(src, {"status": UNAVAILABLE, "label": LABELS[UNAVAILABLE]})
    econ = agro.get("econ") or {}
    for row in econ.get("crops") or []:
        row["policy_drought"] = evidence.get("tp", {})
        row["policy_heat"] = evidence.get("t2m", {})
        row["money_basis"] = "сценарная оценка на вероятностях прогноза; не гарантия дохода"
    return agro
