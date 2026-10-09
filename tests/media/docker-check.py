#!/usr/bin/env python3
"""Test the built image's default start, active stop, restart, and event-end lifecycle."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="breadcast-studio:latest")
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[2] / ".runtime" / "docker-lifecycle.json")
    args = parser.parse_args()
    name = "breadcast-lifecycle-" + uuid.uuid4().hex[:8]
    report = {"image": args.image, "checks": [], "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def docker(*command):
        return subprocess.check_output(["docker", *command], text=True).strip()

    def inspect():
        return json.loads(docker("inspect", name))[0]

    def wait(check, description, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = check()
                if value:
                    return value
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.2)
        raise AssertionError("Timed out: " + description)

    def record(name, **values):
        report["checks"].append({"name": name, **values})
        print(name, flush=True)

    def access():
        return json.loads(docker("exec", name, "cat", "/var/lib/breadcast-studio/access.json"))

    url = None

    def call(path, body=None):
        if path in ('/api/actions','/api/program','/api/replays','/api/replay-cancel') and body is not None and 'expected' not in body:
            state = call('/api/status')
            c = state['control']; p = state['program']; slot = body.get('args', body).get('slot', p['primary_slot'])
            source = next((s for s in state['cameras'] if s['slot'] == slot and s.get('epoch')), None)
            body['expected'] = {'run_id': c['run_id'], 'context_revision': c['context_revision'],
                'control_revision': c['control_revision'], 'program_revision': p['revision'],
                'sources': [{'slot': slot, 'source_path': source['source_path'], 'epoch': source['epoch']}] if source else []}
        request = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    try:
        # No command override: running the image must start the experiment.
        docker("run", "-d", "--name", name, "--stop-timeout", "60", "-p", "127.0.0.1::8080", args.image)
        wait(lambda: inspect()["State"].get("Health", {}).get("Status") == "healthy", "default startup")
        info = inspect()
        port = info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        url = "http://127.0.0.1:" + port
        assert info["Config"]["User"] == "studio"
        assert not info["HostConfig"]["Privileged"]
        first = call("/healthz")["program"]["frames_written"]
        time.sleep(0.4)
        assert call("/healthz")["program"]["frames_written"] > first
        record("default_image_starts_healthy_program", user=info["Config"]["User"])
        docker("exec", "-d", name, "breadcast-studio", "sample", "--count", "5")
        wait(lambda: len(call("/api/status")["cameras"]) == 5 and
             all(c.get("buffer_ready") for c in call("/api/status")["cameras"]), "five sample publishers")
        state = call("/api/status")
        call("/api/program", {"action": "live", "slot": 1, "revision": state["program"]["revision"]})
        wait(lambda: call("/api/status")["program"]["actual"] == "LIVE", "sample program")
        # Docker needs the PID column to map processes into the container.
        processes = [line.split(None, 1)[1] for line in docker("top", name, "-eo", "pid,comm").splitlines()[1:]]
        assert "mediamtx" in processes and processes.count("ffmpeg") >= 6
        record("five_publishers_live_before_stop", processes=processes)
        old_join_code = access()["join_code"]
        old_run = state['control']['run_id']
        call('/api/actions', {'id': 'lifecycle-assisted', 'op': 'mode', 'args': {'mode': 'Assisted'}})
        call('/api/actions', {'id': 'lifecycle-rehearsal', 'op': 'rehearsal', 'args': {'slot': 1}})
        proposal = wait(lambda: next((a for a in call('/api/status')['control']['actions'] if a['state'] == 'Awaiting approval'), None), 'assisted proposal')
        approval = {'id': 'lifecycle-approve', 'op': 'approve', 'args': {'proposal_id': proposal['id'], 'signature': proposal['signature']}}
        assert call('/api/actions', approval)['state'] == 'Finished'
        record('reviewed_approval_before_stop', run_id=old_run, proposal_id=proposal['id'])
        started = time.monotonic()
        docker("stop", name)
        stopped = inspect()["State"]
        assert stopped["Status"] == "exited" and stopped["ExitCode"] == 0 and stopped["Pid"] == 0, stopped
        try:
            call("/healthz")
        except OSError:
            pass
        else:
            raise AssertionError("HTTP endpoint still reachable after stop")
        record("docker_stop_ends_all_container_processes", exit_code=stopped["ExitCode"],
               stop_s=round(time.monotonic() - started, 3), endpoint_closed=True)
        docker("start", name)
        wait(lambda: inspect()["State"].get("Health", {}).get("Status") == "healthy", "restart")
        # Docker may assign a new ephemeral host port on restart.
        port = inspect()["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        url = "http://127.0.0.1:" + port
        assert access()["join_code"] != old_join_code
        state = call("/api/status")
        assert state["occupied"] == 0 and state["program"]["actual"] == "HOLDING"
        assert state['control']['mode'] == 'Manual' and state['control']['run_id'] != old_run
        assert state['control']['actions'] == []
        assert call('/api/actions', approval)['state'] == 'Rejected'
        record("restart_clears_leases_and_rotates_join_code", program="HOLDING", mode='Manual', old_approval_rejected=True)
        try:
            call('/api/event/end', {})
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError('Event ended without its distinct confirmation')
        call("/api/event/end", {"confirm": "End broadcast", "run_id": state["control"]["run_id"]})
        wait(lambda: inspect()["State"]["Status"] == "exited", "event end")
        assert inspect()["State"]["ExitCode"] == 0
        record("operator_event_end_exits_container", exit_code=0)
        report["passed"] = True
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print("Evidence:", args.report)


if __name__ == "__main__":
    main()
