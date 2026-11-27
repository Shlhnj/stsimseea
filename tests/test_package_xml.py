"""Structure checks for src/package.xml (a pasted datasheet definition inside the transformer
broke the 1.0.4 builds on 21 Nov 2026: "Could not find attribute 'type'")."""
import os
import re
import xml.etree.ElementTree as ET

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _root():
    return ET.parse(os.path.join(SRC, "package.xml")).getroot()


def test_package_header():
    p = _root()
    assert p.tag == "package"
    assert p.get("name") == "stsimseea"
    assert re.fullmatch(r"[1-9]\d*\.\d+\.\d+", p.get("version"))


def test_transformer_lists_datasheets_by_name_and_type_only():
    p = _root()
    defined = {ds.get("name") for ds in p.iter("dataSheet") if ds.get("type") is None}
    tr = p.find("transformer")
    assert tr is not None and tr.get("isRunnable") == "True"
    listed = [ds for ds in tr.iter("dataSheet")]
    assert listed, "transformer lists no datasheets"
    for ds in listed:
        assert set(ds.attrib) == {"name", "type"}, f"{ds.get('name')}: {ds.attrib}"
        assert ds.get("type") in ("Input", "Output")
        assert len(ds) == 0, f"{ds.get('name')}: full definition pasted inside the transformer"
        assert ds.get("name") in defined, f"{ds.get('name')} is not defined in the package"
