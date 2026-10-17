"""Build a synthetic Bambu-Studio-shaped 3mf project for tests."""

from __future__ import annotations

import io
import json
import zipfile

from tests.fixtures import FILAMENTS, keychain_gcode

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


def model_settings(with_sequences: bool = True) -> str:
    parts = []
    for i in range(len(FILAMENTS)):
        parts.append(
            f"""    <part id="{i + 1}" subtype="normal_part">
      <metadata key="name" value="part{i + 1}"/>
      <metadata key="filament_id" value="{i + 1}"/>
      <metadata key="extruder" value="{i + 1}"/>
    </part>"""
        )
    plate = ""
    if with_sequences:
        plate = (
            """  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value="Keychain"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
    <metadata key="first_layer_print_sequence" value="1 6 3 2 8 7 4 5 9"/>
  </plate>
"""
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + plate
        + '  <object id="2">\n'
        '    <metadata key="name" value="Keychain"/>\n'
        + "\n".join(parts)
        + "\n  </object>\n</config>\n"
    )


def build_3mf(
    gcode: str | None = None,
    sliced: bool = True,
    sequence_json: bool = False,
) -> bytes:
    gcode = gcode if gcode is not None else keychain_gcode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", MODEL)
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings(), indent=4))
        z.writestr("Metadata/model_settings.config", model_settings())
        z.writestr("Metadata/plate_1.json", "[]")
        z.writestr("Metadata/slice_info.config", "<config/>")
        if sliced:
            z.writestr("Metadata/plate_1.gcode", gcode)
        if sequence_json:
            z.writestr(
                "Metadata/filament_sequence.json",
                json.dumps({"plate_1": {"sequence": [1, 6, 3, 2, 8, 7, 4, 5, 9]}}),
            )
    return buf.getvalue()
