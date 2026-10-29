#!/usr/bin/env python3
"""Unattended OSMH reconciliation of explicitly tagged instances only.

With --deploy, provision the Function and scheduler using local deployer credentials.
Otherwise reconcile locally with --profile, or in OCI Functions with --auth resource_principal.
No user selection, tagging, or terminated-instance deletion occurs in this entrypoint.
"""
import sys
import onboard_osmh


def worker_arguments(argv):
    forbidden = {"--workflow", "--cleanup-only", "--unregister-all", "--unregister-instance-ids",
                 "--interactive", "--instance-ids", "--all"}
    if any(arg.split("=", 1)[0] in forbidden for arg in argv):
        raise SystemExit("The worker always selects tagged instances and never unregisters instances. "
                         "Use onboard_osmh.py --cleanup-only for separately approved cleanup.")
    return [*argv, "--workflow", "onboard-tagged", "--all", "--skip-terminated-cleanup",
            "--defer-registration"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--deploy" in argv:
        argv.remove("--deploy")
        if any(value.startswith("resource_principal") for value in argv):
            raise SystemExit("Deployment is a local administrator operation; the scheduled Function only reconciles.")
        from deploy_osmh_function import main as deploy_main
        deploy_main(argv)
    else:
        onboard_osmh.main(worker_arguments(argv))


if __name__ == "__main__":
    main()
