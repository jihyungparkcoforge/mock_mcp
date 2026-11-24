"""Core behavior must remain usable when provider SDKs are unavailable."""

import subprocess
import sys


def test_core_imports_and_construction_do_not_require_sdks_or_credentials():
    code = """
import importlib.abc
import sys

class NoProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'anthropic', 'mcp', 'dotenv'}:
            raise ImportError('External SDK not available: ' + fullname)

sys.meta_path.insert(0, NoProviders())
from equipment_mcp.workflow import EquipmentWorkflow
from equipment_mcp.policy import validate_decision
from equipment_mcp.equipment import get_item_catalog
from equipment_mcp.repositories import MemoryEquipmentData

assert validate_decision({}, [])
class Unused:
    def __getattr__(self, name):
        raise AssertionError('Constructor accessed dependency: ' + name)

workflow = EquipmentWorkflow(Unused(), Unused())
data = MemoryEquipmentData({'catalog': {
    'items': {'headset': {'price': 120}}, 'currency': 'USD', 'auto_approve_limit': 2000,
}})
assert get_item_catalog('headset', data=data)['price'] == 120
assert not any(n.split('.')[0] in {'anthropic', 'mcp', 'dotenv'} for n in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_application_modules_do_not_require_benchmark_package():
    code = """
import importlib
import importlib.abc
import pkgutil
import sys
import equipment_mcp

class NoBench(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'bench':
            raise ImportError('Benchmark package unavailable: ' + fullname)

sys.meta_path.insert(0, NoBench())
for module in pkgutil.walk_packages(equipment_mcp.__path__, equipment_mcp.__name__ + '.'):
    importlib.import_module(module.name)
assert not any(name.split('.')[0] == 'bench' for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
