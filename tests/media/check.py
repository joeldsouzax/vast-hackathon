"""Exercise real MediaMTX/FFmpeg output with explicitly labeled sample sources."""
from __future__ import annotations

import argparse
import array
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import uuid

from media import stop_process
from studio import request_json

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def wait_for(check, description, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, KeyError) as error:
            last = error
        time.sleep(0.2)
    raise AssertionError(f"Timed out: {description}; last error: {last}")


def control_expected(snapshot, args):
    control, program = snapshot['control'], snapshot['program']
    slots = {args.get('slot', program['primary_slot'])}
    asset = next((r for r in snapshot['replays'] if r['id'] == args.get('replay_id')), None)
    if asset:
        slots.update(int(s['source_id'].removeprefix('camera-')) for s in asset['validation']['plan']['shots'])
    return {'run_id': control['run_id'], 'context_revision': control['context_revision'], 'control_revision': control['control_revision'],
            'program_revision': program['revision'], 'sources': [{'slot': c['slot'], 'source_path': c['source_path'], 'epoch': c['epoch']}
            for slot in sorted(slots) for c in snapshot['cameras'] if c['slot'] == slot and c.get('epoch')]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path,
                        default=Path(os.environ.get("BREADCAST_RUNTIME", str(ROOT / ".runtime"))) / "evidence")
    parser.add_argument("--port", type=int, default=20080)
    parser.add_argument("--port-offset", type=int, default=14000)
    parser.add_argument("--one-camera-seconds", type=int, default=5)
    parser.add_argument("--sustained-seconds", type=int, default=15)
    parser.add_argument("--file", type=Path, help="Use the repository sample MP4 for the primary source")
    parser.add_argument("--foundation-config",type=Path)
    args = parser.parse_args()
    run_started = time.monotonic()
    folder = (args.evidence / uuid.uuid4().hex).resolve()
    folder.mkdir(parents=True, mode=0o700)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests" / "unit"), "-p", "test_leases.py"], cwd=ROOT, check=True)
    runtime = folder / "runtime"
    log = open(folder / "server.log", "wb")
    server = subprocess.Popen([sys.executable, str(APP / "studio.py"), "serve", "--quiet",
                               "--port", str(args.port), "--port-offset", str(args.port_offset),
                               "--runtime", str(runtime), "--delay", "3", "--program-proof",str(folder/"encoder-program.mkv"),
                               "--public-url", f"http://localhost:{args.port}"]+(["--foundation-config",str(args.foundation_config)] if args.foundation_config else []), stdout=log, stderr=log)
    publishers, readers = [], []
    report = {"mode": "sample media; not physical phones or live provider integration", "checks": []}

    def record(name, **values):
        report["checks"].append({"name": name, **values})
        print(name, flush=True)

    try:
        access = wait_for(lambda: json.loads((runtime / "access.json").read_text()), "server access file")
        url = access["public_url"]
        def call(path, data=None):
            if data is not None and path in ('/api/program','/api/replays','/api/replay-cancel') and 'expected' not in data:
                snapshot = request_json(url+'/api/status')
                data = {**data, 'expected': control_expected(snapshot, data)}
            return request_json(url + path, data)
        def status():
            return call("/api/status")
        def program(action, **values):
            return call("/api/program", {"action": action, "revision": status()["program"]["revision"], **values})
        def start_sample(file=None):
            command = [sys.executable, str(APP / "studio.py"), "sample", "--runtime", str(runtime)]
            if file:
                command += ["--file", str(file.resolve())]
            child_log = open(folder / f"publisher-{len(publishers)+1}.log", "wb")
            process = subprocess.Popen(command, stdout=child_log, stderr=child_log)
            child_log.close()
            publishers.append(process)
            return process
        def expect_rejected(path, data, expected=409):
            try:
                call(path, data)
            except urllib.error.HTTPError as error:
                body = json.loads(error.read())
                assert error.code == expected, (error.code, body)
                return body["error"]
            raise AssertionError("Request unexpectedly succeeded")

        wait_for(lambda: call("/healthz")["program"]["frames_written"] > 5, "persistent output")
        start_sample(args.file)
        wait_for(lambda: len(status()["cameras"]) == 1 and status()["cameras"][0].get("buffer_seconds", 0) > 5
                 and status()["cameras"][0].get("buffer_ready"),
                 "one-camera decoded buffer")
        command_began=time.monotonic()
        program("live", slot=1)
        wait_for(lambda: status()["program"]["actual"] == "LIVE", "one-camera live program")
        command_application_s=time.monotonic()-command_began
        encoder_pid = status()["program"]["encoder_pid"]
        started = time.monotonic()
        while time.monotonic() - started < args.one_camera_seconds:
            assert status()["program"]["actual"] == "LIVE", status()["program"]
            time.sleep(0.25)
        record("one_camera_program", duration_s=round(time.monotonic() - started, 2), encoder_pid=encoder_pid,
               command_application_s=command_application_s,application_clock='Local request through encoded-frame acknowledgement; polling bound',
               normalized_media_age_s=status()["program"]["normalized_media_age_s"])
        reason = expect_rejected("/api/program", {"action": "holding", "revision": status()["program"]["revision"] + 0.2})
        assert "integer" in reason and status()["program"]["requested"] == "LIVE"
        record("invalid_revision_type_rejected")

        live_audio = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp",
                                     "-i", f"rtsp://127.0.0.1:{access['rtsp_port']}/program", "-t", "2", "-vn",
                                     "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"],
                                    capture_output=True, timeout=12, check=True).stdout
        live_samples = array.array("h", live_audio)
        live_rms = math.sqrt(sum(s * s for s in live_samples) / len(live_samples)) / 32768
        assert live_rms > 0.01, live_rms
        record("designated_live_audio_reaches_program", normalized_rms=live_rms)

        recording = folder / "program.mkv"
        recorder_log = open(folder / "recorder.log", "wb")
        recorder = subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                     "-rtsp_transport", "tcp", "-i", f"rtsp://127.0.0.1:{access['rtsp_port']}/program",
                                     "-c", "copy", str(recording)], stderr=recorder_log)
        recorder_log.close()
        readers.append(recorder)

        # Use separate publishers so removing one does not stop the other fixtures.
        for count in range(2, 6):
            start_sample()
            wait_for(lambda: status()["occupied"] == count, f"reserve camera {count}")
        wait_for(lambda: len(status()["cameras"]) == 5 and
                 all(c.get("buffer_seconds", 0) > 4 and c["state"] == "ACTIVE" and c.get("buffer_ready")
                     for c in status()["cameras"]),
                 "five decoded cameras", timeout=45)
        record("five_active_cameras", slots=[c["slot"] for c in status()["cameras"]])
        full = expect_rejected("/api/leases", {"code": access["join_code"], "client": uuid.uuid4().hex})
        assert "five" in full
        record("sixth_join_rejected")
        reason = expect_rejected("/api/program", {"action": "live", "revision": status()["program"]["revision"], "slot": 2})
        assert "sync" in reason
        record("uncalibrated_seamless_cut_rejected")

        fifth = status()["cameras"][-1]
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-re",
                   "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5", "-an", "-c:v", "libx264",
                   "-threads", "1", "-preset", "ultrafast", "-t", "1", "-f", "rtsp", "-rtsp_transport", "tcp",
                   f"rtsp://publisher:invalid@127.0.0.1:{access['rtsp_port']}/{fifth['source_path']}"]
        rejected = subprocess.run(command, capture_output=True, timeout=10)
        assert rejected.returncode != 0 and b"401" in rejected.stderr, rejected.stderr.decode()
        record("direct_unauthorized_publish_rejected")

        old_manifest=None
        if args.foundation_config:
            import sqlite3
            def old_archive():
                with sqlite3.connect(f"file:{runtime/'foundation.sqlite'}?mode=ro",uri=True) as database:
                    row=database.execute("SELECT body FROM records WHERE kind='chunk' AND available=1 AND json_extract(body,'$.source.source_id')=? LIMIT 1",(fifth['source_path'],)).fetchone()
                    return json.loads(row[0]) if row else None
            wait_for(old_archive,'finalized original Camera 5 archive',timeout=15)
            old_manifest=old_archive()
        request_json(url + f"/api/lease/{fifth['lease_id']}/release", {}, token=fifth['token'])
        wait_for(lambda: status()["occupied"] == 4, "released slot")
        start_sample()
        wait_for(lambda: status()["occupied"] == 5 and status()["cameras"][-1].get("buffer_seconds", 0) > 4,
                 "replacement camera")
        assert status()["cameras"][-1]["source_path"] != fifth["source_path"]
        old = subprocess.run(command, capture_output=True, timeout=10)
        assert old.returncode != 0
        record("publisher_fenced_and_slot_reused")

        render_results = []
        ready = None
        for speed, zoom in [(1, 1), (0.5, 1.5), (2, 1)]:
            wait_for(lambda:next((c for c in status()['cameras'] if c['slot']==1 and c.get('current_epoch_buffer_seconds',0)>4.2),None),
                     "four seconds in current timestamp epoch")
            job = call("/api/replays", {"slot": 1, "seconds": 4, "speed": speed, "zoom": zoom})
            result = wait_for(lambda: next((j for j in status()["jobs"] if j["id"] == job["id"] and j["state"] != "rendering"), None),
                              "rendered replay")
            assert result["state"] == "ready", result
            ready = next(r for r in status()["replays"] if r["id"] == job["id"])
            assert ready["validation"]["decode_passed"]
            render_results.append({"speed": speed, "zoom": zoom, "duration_s": ready["duration_s"], "render_s": result["render_s"]})
        record("replay_speed_and_crop_presets", results=render_results)

        # Replay stays silent. Capture and the delayed position keep advancing.
        slow = next(r for r in status()["replays"] if r["speed"] == 0.5)
        before_sequence = status()["program"]["actual_target"]["sequence"]
        program("replay", replay_id=slow["id"])
        wait_for(lambda: status()["program"]["actual"] == "REPLAY", "replay airtime")
        duplicate = expect_rejected("/api/program", {"action": "replay", "replay_id": slow["id"],
                                                     "revision": status()["program"]["revision"] - 1})
        assert "revision" in duplicate
        record("stale_command_rejected")
        silence = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp",
                                  "-i", f"rtsp://127.0.0.1:{access['rtsp_port']}/program", "-t", "2", "-vn",
                                  "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"],
                                 capture_output=True, timeout=12, check=True).stdout
        samples = array.array("h", silence[-48000:])
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
        assert rms < 0.005, rms
        record("replay_audio_muted", normalized_rms=rms)
        start = time.monotonic()
        program("live")
        wait_for(lambda: status()["program"]["actual"] == "LIVE", "return to live")
        returned = status()["program"]
        assert returned["actual_target"]["sequence"] > before_sequence + 15
        assert returned["normalized_media_age_s"] < 3.3
        assert returned["encoder_pid"] == encoder_pid
        record("return_to_current_delayed_live", control_to_encoder_s=time.monotonic() - start,
               source_sequence_before=before_sequence, source_sequence_after=returned["actual_target"]["sequence"])

        program("replay", replay_id=ready["id"])
        wait_for(lambda: status()["program"]["actual"] == "REPLAY", "fast replay")
        wait_for(lambda: status()["program"]["actual"] == "LIVE", "automatic return to live", timeout=10)
        record("automatic_replay_return")
        program("live", slot=2, independent=True)
        wait_for(lambda: status()["program"]["actual"] == "LIVE", "independent view")
        program("live", slot=1, independent=True)
        record("explicit_independent_view_change")

        started = time.monotonic()
        memory_samples = []
        while time.monotonic() - started < args.sustained_seconds:
            current = status()
            assert current["occupied"] == 5 and current["program"]["actual"] == "LIVE", current
            assert current["program"]["encoder_pid"] == encoder_pid and not current["program"]["error"]
            memory_samples.append(sum(c.get("buffer_bytes", 0) for c in current["cameras"]))
            time.sleep(0.5)
        record("five_source_sustained_output", duration_s=time.monotonic() - started,
               peak_jpeg_buffer_bytes=max(memory_samples), encoder_pid=encoder_pid)
        # Losing the primary feed must reach holding after the delay buffer drains.
        stop_process(publishers[0])
        wait_for(lambda: status()["program"]["actual"] == "HOLDING", "source-loss holding", timeout=15)
        record("primary_source_loss_to_holding")
        recorder.send_signal(__import__("signal").SIGINT)
        recorder.wait(timeout=10)
        assert recording.stat().st_size > 10000
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(recording),
                        "-f", "null", "-"], check=True, capture_output=True, timeout=30)
        probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                                   "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                                                   "-of", "json", str(recording)], timeout=20))
        timestamps = [float(f["best_effort_timestamp_time"]) for f in probe["frames"]]
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        assert len(timestamps) > 100
        record("rtsp_program_output_decodes", clock="RTSP reader; distinct from encoder program PTS",zero_timestamp_steps=sum(g==0 for g in gaps),
               backward_timestamp_steps=sum(g<0 for g in gaps),min_frame_gap_s=min(gaps),frames=len(timestamps),
               duration_s=timestamps[-1] - timestamps[0], max_frame_gap_s=max(gaps), output=str(recording))
        originals = list((runtime / "recordings" / "camera").rglob("*.mp4"))
        if args.foundation_config:
            import sqlite3
            database=sqlite3.connect(runtime/"foundation.sqlite")
            row=database.execute("SELECT body FROM records WHERE kind='chunk' AND available=1 LIMIT 1").fetchone()
            assert row, status()['foundation']
            manifest=json.loads(row[0]);database.close()
            closed=runtime/"foundation-media"/manifest['media']['key']
            foundation=status()['foundation']
            assert foundation['jobs'].get('completed',0)>0, foundation
            assert foundation['jobs'].get('expired',0)>0, foundation
            record("background_foundation_ingestion",diagnostics=foundation,retained_manifest=manifest)
            from foundation import Foundation
            from foundation_records import FoundationSettings,SourceEpoch,Interval
            from foundation_storage import inspect_video
            archive=Foundation(runtime,foundation['run_id'],FoundationSettings.load(args.foundation_config),readonly=True)
            try:
                resolved=archive.resolve(SourceEpoch.model_validate(old_manifest['source']),Interval.model_validate(old_manifest['native']))
                decoded=sum(inspect_video(chunk['path'])['frames'] for chunk in resolved['chunks'])
                assert decoded>0 and old_manifest['source']['source_id']!=status()['cameras'][-1]['source_path']
                record('archive_after_disconnect_and_slot_reuse',source=old_manifest['source'],native=old_manifest['native'],decoded_frames=decoded)
            finally:archive.close()
        else:
            assert originals
            closed = sorted(originals, key=lambda f: f.stat().st_mtime)[0]
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(closed),
                        "-f", "null", "-"], check=True, capture_output=True, timeout=15)
        record("original_camera_segment_decodes", output=str(closed))
        black=subprocess.run(["ffmpeg","-nostdin","-v","info","-i",str(recording),"-vf","blackdetect=d=0.1:pix_th=0.01", "-an","-f","null","-"],capture_output=True,timeout=60,check=True)
        assert b"black_start:" not in black.stderr, black.stderr.decode()[-2000:]
        record("no_unexpected_black_frames",duration_s=timestamps[-1]-timestamps[0])
        report["versions"] = json.loads((runtime / "versions.json").read_text())
        server.send_signal(__import__('signal').SIGTERM)
        server.wait(timeout=30)
        native=folder/'encoder-program.mkv'
        import av
        native_timestamps=[]
        with av.open(str(native)) as container:
            for frame in container.decode(video=0):native_timestamps.append(float(frame.pts*frame.time_base))
        native_gaps=[b-a for a,b in zip(native_timestamps,native_timestamps[1:])]
        assert min(native_gaps)>0 and max(native_gaps)<.14 and native_timestamps[-1]-native_timestamps[0]>=args.sustained_seconds
        record('encoder_program_pts_continuous',frames=len(native_timestamps),duration_s=native_timestamps[-1]-native_timestamps[0],
               max_frame_gap_s=max(native_gaps),output=str(native),clock='Encoded packets before RTSP transport; one encoder')
        late=[json.loads(line) for line in (runtime/'program-history.jsonl').read_text().splitlines()
              if json.loads(line)['kind']=='encoder_schedule_late']
        assert not late,late
        record('encoder_schedule_continuous',late_events=len(late),threshold_s=.5)
        report["passed"] = True
    except BaseException as error:
        report.update(passed=False, failure=str(error))
        try:
            report["failure_status"] = status()
            report["failure_status"].pop("join_url", None)
            report["failure_status"].pop("broadcast_url", None)
        except (OSError, UnboundLocalError):
            pass
        raise
    finally:
        for child in publishers + readers:
            stop_process(child)
        stop_process(server)
        log.close()
        report['elapsed_s'] = time.monotonic() - run_started
        (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Evidence: {folder / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
