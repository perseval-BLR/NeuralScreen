"""Execute the production send block: the capture round trip only where it is read.

No GPU needed. The worker/guide boundaries are mocked; the actual main-loop
statements are compiled from main.py so duplicate calls cannot hide in a helper.

The contract, per frame:

* CPU optical flow (DIS): one prepared capture, then one guide update that
  reads the gray it latched - in that order;
* NVOFA: no capture round trip at all. The worker makes the motion field and
  decides the scene cut from the gray it has just captured
  (FRAME_FLAG_WORKER_SCENE); the loop hands off, with zero motion and a reset
  only for what it knows (a fresh start);
* NR off without Frame Generation: no capture and no guides - nothing reads
  them;
* NR off WITH Frame Generation: the presenter interpolates the frames it is
  handed, so the reset flag must not be set on every frame (#104) - the
  hand-off (NVOFA) or real guides (DIS), never the zero guide;
* NS_WORKER_SCENE=0: the old way everywhere, a prepared capture every frame.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def production_block():
    tree = ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
              and node.body and isinstance(node.body[0], ast.Expr)
              and isinstance(node.body[0].value, ast.Call)
              and isinstance(node.body[0].value.func, ast.Name)
              and node.body[0].value.func.id == 'check_worker']
    assert len(blocks) == 1, 'worker/guide send path runs twice per frame'
    loop = ast.For(target=ast.Name(id='_frame', ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(0)], ctx=ast.Load()),
                   body=blocks, orelse=[])
    return compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                   'production-send-block', 'exec')


def run(code, *, hardware: bool, bypass: bool, fg: bool = False,
        worker_scene: bool = True):
    """One frame through the real block. Returns (calls, send, guides)."""
    calls = []
    frame = object()
    guide = SimpleNamespace(motion=object(), reset=False)

    def capture(*args):
        calls.append('capture')

    def process(*args, **kwargs):
        calls.append('guides')
        assert calls[-2:] == ['capture', 'guides'], 'guides read a gray no capture latched'
        assert kwargs['gray'] is frame
        assert kwargs['compute_motion'] is not hardware
        return guide

    def handoff():
        calls.append('handoff')
        return guide

    guides = SimpleNamespace(process=Mock(side_effect=process),
                             handoff=Mock(side_effect=handoff),
                             zero_guide=Mock(return_value=SimpleNamespace(motion=object(), reset=True)),
                             forget=Mock())
    worker = object()
    st = SimpleNamespace(worker=worker, worker_logs=[], reader=object(),
         cfg={'motion_backend': 'nvofa', 'frame_generation': fg}, gray_active=True,
         frame_index=7, pts=70, guides=guides, shm=SimpleNamespace(read_gray=lambda: frame),
         work_frame=None, pending_shot=None, recorder=None, motion_small=True,
         dda_mode=True, split_pos=0, lang='en', guide_fails=0)
    status = SimpleNamespace(failed=False, worker=worker, update=lambda *a: hardware)
    send = Mock()
    # The loop asks fg_multiplier for the step this frame (main.py) - the
    # display-matched multiplier. The block under test owns only the send, so
    # the chooser is a stub here, and the test checks that its answer is what
    # the frame actually carries.
    fg_mult = Mock(return_value=2)
    namespace = dict(st=st, bypass=bypass, motion_status=status, time=time, sys=sys,
                     check_worker=Mock(), prepare_capture=capture, send_frame=send,
                     fg_multiplier=fg_mult, last_fps=60.0,
                     _perf=Mock(), EARLY_REPLY=True, WORKER_SCENE=worker_scene)
    exec(code, namespace)
    assert send.call_count == 1, 'one frame, one send'
    assert fg_mult.call_count == 1, 'the loop did not ask for the FG step'
    assert send.call_args.kwargs['frame_multiplier'] == 2, \
        'the FG step the chooser returned is not the one sent'
    # The loop asks for the answer as soon as the frame is queued; the
    # worker decides where that is safe (FRAME_FLAG_EARLY_REPLY).
    assert send.call_args.kwargs['early_reply'] is True
    return calls, send, guides


def expect(case: str, calls, send, *, sequence, prepared, worker_scene):
    kw = send.call_args.kwargs
    assert calls == sequence, f'{case}: {calls}, expected {sequence}'
    assert kw['prepared'] is prepared, f"{case}: prepared={kw['prepared']}"
    assert kw['worker_scene'] is worker_scene, f"{case}: worker_scene={kw['worker_scene']}"


def main():
    code = production_block()

    calls, send, _ = run(code, hardware=False, bypass=False)
    expect('DIS', calls, send, sequence=['capture', 'guides'], prepared=True,
           worker_scene=False)

    calls, send, guides = run(code, hardware=True, bypass=False)
    expect('NVOFA', calls, send, sequence=['handoff'], prepared=False,
           worker_scene=True)
    assert guides.process.call_count == 0, 'NVOFA still read the gray on the client'

    for hardware in (True, False):
        calls, send, guides = run(code, hardware=hardware, bypass=True)
        expect(f'NR off ({"NVOFA" if hardware else "DIS"})', calls, send,
               sequence=[], prepared=False, worker_scene=True)
        guides.forget.assert_called_once()
        guides.zero_guide.assert_called_once()

    # NR off WITH Frame Generation: never the zero guide and its per-frame
    # reset (#104). The reset also reaches FG, which resets on fh.reset
    # regardless of the bypass flag.
    calls, send, guides = run(code, hardware=True, bypass=True, fg=True)
    expect('NR off + FG (NVOFA)', calls, send, sequence=['handoff'],
           prepared=False, worker_scene=True)
    calls, send, guides = run(code, hardware=False, bypass=True, fg=True)
    expect('NR off + FG (DIS)', calls, send, sequence=['capture', 'guides'],
           prepared=True, worker_scene=False)
    assert guides.zero_guide.call_count == 0, \
        'NR off + FG fell back to zero_guide: the field FG interpolates from is zero and reset'
    assert send.call_args.args[4] is False, \
        'NR off + FG sent a reset: FG resets on fh.reset and would interpolate nothing'

    # NS_WORKER_SCENE=0: the round trip every frame, as before.
    calls, send, _ = run(code, hardware=True, bypass=False, worker_scene=False)
    expect('NVOFA, switched off', calls, send, sequence=['capture', 'guides'],
           prepared=True, worker_scene=False)
    calls, send, _ = run(code, hardware=True, bypass=True, worker_scene=False)
    expect('NR off, switched off', calls, send, sequence=['capture'],
           prepared=True, worker_scene=False)

    print('PASS: a capture round trip only for DIS; NVOFA and NR off hand the '
          'scene cut to the worker; NR off + FG never resets every frame; '
          'NS_WORKER_SCENE=0 restores the round trip')


if __name__ == '__main__':
    main()
