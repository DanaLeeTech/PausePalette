"""Build a synthetic Bambu-Studio-shaped 3mf project for tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

from tests.fixtures import FILAMENTS, HEAD_LAYERS, keychain_gcode

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="2" type="model">
      <mesh>
        <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>
</model>
"""


def project_settings() -> dict:
    n = len(FILAMENTS)
    return {
        "printer_model": "Bambu Lab A1",
        "printer_settings_id": "Bambu Lab A1 0.4 nozzle",
        "extruder_count": "1",
        "filament_colour": list(FILAMENTS),
        "filament_type": ["PLA"] * n,
        "filament_settings_id": ["Bambu PLA Basic"] * n,
        "nozzle_temperature": ["220"] * n,
        "filament_flow_ratio": ["0.98"] * n,
        "filament_diameter": ["1.75"] * n,
        "filament_vendor": [f"Vendor{i}" for i in range(n)],
        # a per-printer list of the same length that must NOT be permuted
        "printer_fake_per_printer": [f"p{i}" for i in range(n)],
        "layer_height": "0.2",
    }


def _stored_sequences() -> tuple[str, str, int]:
    """Bambu's per-layer print order: layer 1, layers 2-3, then the rest."""
    blocks = [[layer, layer, *[t + 1 for t in HEAD_LAYERS[layer]]]
              for layer in sorted(HEAD_LAYERS)[1:]]
    blocks.append([4, 2 ** 31 - 2, *[t + 1 for t in HEAD_LAYERS[1]]])
    other = " ".join(str(x) for block in blocks for x in block)
    first = " ".join(str(t + 1) for t in HEAD_LAYERS[1])
    return first, other, len(blocks)


def _plate_block(
    gcode: bool = True,
    object_id: int | None = None,
    sequences: tuple[str, str, int] | None = None,
) -> str:
    first, other, count = sequences or _stored_sequences()
    instance = (
        f'    <model_instance><metadata key="object_id" value="{object_id}"/></model_instance>\n'
        if object_id is not None
        else ""
    )
    gcode_line = (
        '    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>\n' if gcode else ""
    )
    return (
        f"""  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value="Keychain"/>
{gcode_line}    <metadata key="first_layer_print_sequence" value="{first}"/>
    <metadata key="other_layers_print_sequence" value="{other}"/>
    <metadata key="other_layers_print_sequence_nums" value="{count}"/>
{instance}  </plate>
"""
    )


def model_settings(with_sequences: bool = True, ref_base: int = 1) -> str:
    """Project model settings; ``ref_base`` picks 1-based (Bambu) or 0-based refs."""
    parts = []
    for i in range(len(FILAMENTS)):
        parts.append(
            f"""    <part id="{i + 1}" subtype="normal_part">
      <metadata key="name" value="part{i + 1}"/>
      <metadata key="filament_id" value="{i + ref_base}"/>
      <metadata key="extruder" value="{i + ref_base}"/>
    </part>"""
        )
    plate = _plate_block(gcode=True) if with_sequences else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + plate
        + '  <object id="2">\n'
        '    <metadata key="name" value="Keychain"/>\n'
        + "\n".join(parts)
        + "\n  </object>\n</config>\n"
    )


#: the geometry fixture mirrors the reference print: one object whose parts say
#: which filament each layer prints with — the image colours 2-8 on the first
#: three layers, black (1) the whole way up, white (9) down to layer 6 and dark
#: grey (6) again near the top. (filament, z from, z to) in mm.
GEOMETRY_MESH = "3D/Objects/object_7.model"
GEOMETRY_PARTS = [
    (1, 0.0, 5.0),
    (2, 0.0, 0.6),
    (3, 0.0, 0.6),
    (4, 0.0, 0.6),
    (5, 0.0, 0.6),
    (6, 0.0, 0.6),
    (6, 4.2, 5.0),
    (7, 0.0, 0.6),
    (8, 0.0, 0.6),
    (9, 0.0, 0.6),
    (9, 0.6, 1.2),
]
#: the colour print order the reference project stores (layer 1, layers 2-3,
#: then everything above)
GEOMETRY_ORDERS = (
    "1 6 3 2 8 7 4 5 9",
    "2 2 5 9 8 7 4 1 6 3 2 3 3 1 6 3 2 8 7 4 5 9 4 2147483646 1 6 9 4 5 2 7 8 3",
    3,
)


def _geometry_object_model() -> str:
    objects = []
    for i, (_colour, z_lo, z_hi) in enumerate(GEOMETRY_PARTS):
        objects.append(
            f"""    <object id="{i + 10}" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="{z_lo}"/>
          <vertex x="1" y="0" z="{z_lo}"/>
          <vertex x="0" y="1" z="{z_hi}"/>
        </vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
      </mesh>
    </object>"""
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        "  <resources>\n" + "\n".join(objects) + "\n  </resources>\n</model>\n"
    )


def _geometry_main_model() -> str:
    comps = "\n".join(
        f'      <component p:path="/{GEOMETRY_MESH}" objectid="{i + 10}" '
        f'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        for i in range(len(GEOMETRY_PARTS))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">\n'
        "  <resources>\n"
        '    <object id="2" type="model">\n'
        f"      <components>\n{comps}\n      </components>\n"
        "    </object>\n"
        "  </resources>\n"
        '  <build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>\n'
        "</model>\n"
    )


def geometry_3mf() -> bytes:
    """An unsliced project whose per-layer colour use is in the model geometry."""
    parts = "\n".join(
        f"""    <part id="{i + 10}" subtype="normal_part">
      <metadata key="name" value="part{i + 1}"/>
      <metadata key="extruder" value="{colour}"/>
    </part>"""
        for i, (colour, _z_lo, _z_hi) in enumerate(GEOMETRY_PARTS)
    )
    settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + _plate_block(gcode=False, object_id=2, sequences=GEOMETRY_ORDERS)
        + '  <object id="2">\n'
        '    <metadata key="name" value="Keychain"/>\n'
        + parts
        + "\n  </object>\n</config>\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", _geometry_main_model())
        z.writestr(GEOMETRY_MESH, _geometry_object_model())
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings(), indent=4))
        z.writestr("Metadata/model_settings.config", settings)
        z.writestr("Metadata/plate_1.json", "[]")
    return buf.getvalue()


def build_3mf(
    gcode: str | None = None,
    sliced: bool = True,
    sequence_json: bool = False,
    ref_base: int = 1,
    with_sequences: bool = True,
) -> bytes:
    gcode = gcode if gcode is not None else keychain_gcode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", MODEL)
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings(), indent=4))
        z.writestr("Metadata/model_settings.config",
                   model_settings(with_sequences=with_sequences, ref_base=ref_base))
        z.writestr("Metadata/plate_1.json", "[]")
        z.writestr("Metadata/slice_info.config", "<config/>")
        if sliced:
            z.writestr("Metadata/plate_1.gcode", gcode)
            z.writestr(
                "Metadata/plate_1.gcode.md5",
                hashlib.md5(gcode.encode("utf-8")).hexdigest().upper(),
            )
        if sequence_json:
            z.writestr(
                "Metadata/filament_sequence.json",
                json.dumps({"plate_1": {"sequence": [1, 6, 3, 2, 8, 7, 4, 5, 9]}}),
            )
    return buf.getvalue()
