"""Command line inventory report, no web server needed.

    python scripts/list_vms.py
    python scripts/list_vms.py --power-state poweredOff

Reads the same VCENTER_* environment variables as the API.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import get_settings  # noqa: E402
from app.vsphere import VCenterUnavailableError, VSphereClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="List VMs from vCenter")
    parser.add_argument("--power-state", choices=["poweredOn", "poweredOff", "suspended"])
    args = parser.parse_args()

    client = VSphereClient.from_settings(get_settings())
    try:
        vms = client.list_vms()
    except VCenterUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    if args.power_state:
        vms = [v for v in vms if v.power_state == args.power_state]

    print(f"{'NAME':<28}{'POWER':<14}{'CPU':>4}{'MEM(MB)':>9}  HOST")
    for vm in sorted(vms, key=lambda v: v.name):
        print(f"{vm.name:<28}{vm.power_state:<14}{vm.num_cpu or '-':>4}{vm.memory_mb or '-':>9}  {vm.host or '-'}")
    print(f"\n{len(vms)} VM(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
