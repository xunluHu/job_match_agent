"""Five evidence-based capability dimensions and dependency-free radar charts."""
import math

AXES = [
    ("technical", "Technical skills"),
    ("experience", "Relevant experience"),
    ("delivery", "Project delivery"),
    ("systems", "Systems & reliability"),
    ("ownership", "Collaboration & ownership"),
]


def capability_schema():
    return {"type": "array", "minItems": 5, "maxItems": 5, "items": {
        "type": "object", "required": ["axis", "required_level", "candidate_level", "requirement", "assessment", "resume_ref", "job_evidence_ref"],
        "properties": {
            "axis": {"type": "string", "enum": [key for key, _ in AXES]},
            "required_level": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
            "candidate_level": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
            "requirement": {"type": "string"}, "assessment": {"type": "string"},
            "resume_ref": {"type": ["string", "null"]},
            "job_evidence_ref": {"type": ["string", "null"]},
        }}}


def resolve_capabilities(items, resume_map, job_map):
    if not isinstance(items, list) or len(items) != 5 or {item.get("axis") for item in items if isinstance(item, dict)} != {key for key, _ in AXES}:
        raise ValueError("Each job must have exactly five unique capability dimensions.")
    by_axis = {item["axis"]: item for item in items}
    resolved = []
    for key, label in AXES:
        item = dict(by_axis[key], label=label)
        for level, ref, mapping, quote in (("candidate_level", "resume_ref", resume_map, "resume_quote"),
                                            ("required_level", "job_evidence_ref", job_map, "job_quote")):
            value, reference = item.get(level), item.get(ref)
            if value is not None and reference is None:
                # The mirror case: a level with no source is unsupported, so it is shown as unknown, not as a score.
                item.setdefault("normalizations", []).append(f"cleared_{level}_without_{ref}")
                value = None
            if value is None:
                # A source can discuss a topic without establishing a proficiency level.
                # Discard the redundant reference; never infer a score from its presence.
                if reference is not None:
                    item.setdefault("normalizations", []).append(f"cleared_{ref}_for_unknown_{level}")
                item[level] = None
                item[ref] = None
                item[quote] = None
            else:
                if type(value) is not int or not 0 <= value <= 100 or reference not in mapping:
                    raise ValueError("Capability scores require a valid source reference and a 0–100 integer.")
                item[quote] = mapping[reference]
        if not isinstance(item.get("requirement"), str) or not isinstance(item.get("assessment"), str):
            raise ValueError("Capability descriptions must be strings.")
        item["coverage"] = (round(min(1, item["candidate_level"] / item["required_level"]) * 100)
                            if item["candidate_level"] is not None and item["required_level"] not in (None, 0) else None)
        resolved.append(item)
    return resolved


def radar_svg(capabilities):
    """Unknown values are missing points, never plotted as zero capability."""
    cx, cy, radius = 260, 185, 120
    def point(index, level):
        angle = -math.pi/2 + index * 2*math.pi/5
        return cx + radius*level/100*math.cos(angle), cy + radius*level/100*math.sin(angle)
    def coords(points):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 380" role="img" aria-label="Required versus evidenced capabilities">',
           '<rect width="520" height="380" rx="12" fill="#ffffff"/>',
           '<g font-family="Arial,sans-serif" font-size="12" fill="#526174">']
    for level in (25, 50, 75, 100):
        out.append(f'<polygon points="{coords([point(i, level) for i in range(5)])}" fill="none" stroke="#dce4eb"/>')
        out.append(f'<text x="265" y="{cy-radius*level/100+4:.1f}" font-size="10">{level}</text>')
    for i, (_, label) in enumerate(AXES):
        x, y = point(i, 100)
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#dce4eb"/>')
        tx, ty = point(i, 128)
        parts = label.split(" & ") if " & " in label else label.rsplit(" ", 1)
        for n, part in enumerate(parts):
            # All labels are fixed application constants.
            out.append(f'<text x="{tx:.1f}" y="{ty+n*14:.1f}" text-anchor="middle">{part.replace("&", "&amp;")}</text>')
    for field, color in (("required_level", "#d78b22"), ("candidate_level", "#177560")):
        values = [item[field] for item in capabilities]
        if all(value is not None for value in values):
            out.append(f'<polygon points="{coords([point(i,v) for i,v in enumerate(values)])}" fill="{color}" fill-opacity="0.12" stroke="{color}" stroke-width="2"/>')
        else:
            for i, value in enumerate(values):
                nxt = (i+1)%5
                if value is not None and values[nxt] is not None:
                    x1,y1 = point(i,value); x2,y2 = point(nxt,values[nxt])
                    out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="2"/>')
        for i, value in enumerate(values):
            if value is not None:
                x,y = point(i,value)
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
    out += ['<text x="100" y="350" fill="#d78b22">● Job requirement</text>',
            '<text x="290" y="350" fill="#177560">● Resume evidence</text>',
            '<text x="260" y="372" text-anchor="middle" font-size="10">0–100 rubric · missing points = unknown, not zero</text>', '</g></svg>']
    return "".join(out)
