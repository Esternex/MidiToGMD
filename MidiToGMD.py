import argparse
import base64
import gzip
import statistics
import sys

try:
    import mido
except ImportError:
    sys.exit("This script needs mido. Install it with:\n"
              "    pip install mido --break-system-packages")

SPEED_PRESETS_BLOCKS_PER_SEC = {
    "slow": 8.4,
    "normal": 11.2,
    "fast": 14.0,
    "faster": 16.8,
    "fastest": 19.2,
}
SPEED_ENUM = {"normal": 0, "slow": 1, "fast": 2, "faster": 3, "fastest": 4}

UNITS_PER_BLOCK = 30

TRIGGER_Y = 75
START_X = 0
START_Y = 105


def fmt(x):
    """Format a number the way GD level strings expect (no trailing zeros)."""
    if isinstance(x, float):
        s = f"{x:.3f}".rstrip('0').rstrip('.')
        return s if s else "0"
    return str(x)


def extract_notes(midi_path):
    """Return a sorted list of (time_seconds, midi_note) for every note-on
    event in the file, honoring tempo changes anywhere in the file."""
    mid = mido.MidiFile(midi_path)
    events = []
    tempo = 500000
    abs_time = 0.0
    for msg in mido.merge_tracks(mid.tracks):
        abs_time += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == 'set_tempo':
            tempo = msg.tempo
        if msg.type == 'note_on' and msg.velocity > 0:
            events.append((round(abs_time, 6), msg.note))
    events.sort()
    return events


def dedupe_chords(events, window_s):
    """Collapse notes that start within `window_s` of each other into a
    single event, keeping the highest (most audible) note. window_s <= 0
    disables this and keeps every note as its own trigger."""
    if window_s <= 0:
        return events
    out = []
    cluster_t = None
    cluster_notes = []
    for t, note in events:
        if cluster_t is None or t - cluster_t > window_s:
            if cluster_notes:
                out.append((cluster_t, max(cluster_notes)))
            cluster_t = t
            cluster_notes = [note]
        else:
            cluster_notes.append(note)
    if cluster_notes:
        out.append((cluster_t, max(cluster_notes)))
    return out


def fold_pitch(note, center, pitch_range=12):
    """Fold a MIDI note into GD's [-pitch_range, +pitch_range) window
    around `center`, preserving pitch class via octave wrapping instead
    of clipping."""
    span = pitch_range * 2
    diff = note - center
    diff = ((diff + pitch_range) % span) - pitch_range
    return diff


def sfx_trigger(x, y, pitch, sfx_id, volume, reverb, duration):
    reverb_val = 1 if reverb else 0
    return (f"1,3602,2,{fmt(x)},3,{fmt(y)},155,1,36,1,392,{sfx_id},404,0,405,{pitch-1},"
            f"406,{fmt(volume)},407,{reverb_val},421,1,422,0.5,10,0.5,490,{fmt(duration)}")


def start_pos(x, y, speed_enum_val):
    return (f"1,31,2,{fmt(x)},3,{fmt(y)},155,1,36,1,kA2,0,kA3,0,kA8,0,kA4,{speed_enum_val},kA9,1,kA10,0,"
            "kA22,0,kA23,0,kA24,0,kA27,1,kA40,1,kA48,1,kA41,1,kA42,1,kA28,0,kA29,0,kA31,1,kA32,1,"
            "kA36,0,kA43,0,kA44,0,kA45,1,kA46,0,kA47,0,kA33,1,kA34,1,kA35,0,kA37,1,kA38,1,kA39,1,"
            "kA19,0,kA26,0,kA20,0,kA21,0,kA11,0")


HEADER_TEMPLATE = (
    "kS38,1_40_2_125_3_255_11_255_12_255_13_255_4_-1_6_1000_7_1_15_1_18_0_8_1|"
    "1_0_2_102_3_255_11_255_12_255_13_255_4_-1_6_1001_7_1_15_1_18_0_8_1|"
    "1_0_2_102_3_255_11_255_12_255_13_255_4_-1_6_1009_7_1_15_1_18_0_8_1|"
    "1_255_2_255_3_255_11_255_12_255_13_255_4_-1_6_1002_5_1_7_1_15_1_18_0_8_1|"
    "1_40_2_125_3_255_11_255_12_255_13_255_4_-1_6_1013_7_1_15_1_18_0_8_1|"
    "1_40_2_125_3_255_11_255_12_255_13_255_4_-1_6_1014_7_1_15_1_18_0_8_1|"
    "1_255_2_255_3_255_11_255_12_255_13_255_4_-1_6_1005_5_1_7_1_15_1_18_0_8_1|"
    "1_125_2_0_3_255_11_255_12_255_13_255_4_-1_6_1006_5_1_7_1_15_1_18_0_8_1|,"
    "kA13,0,kA15,0,kA16,0,kA14,,kA6,0,kA7,0,kA25,0,kA17,0,kA18,0,kS39,0,"
    "kA2,0,kA3,0,kA8,0,kA4,{speed},kA9,0,kA10,0,kA22,0,kA23,0,kA24,0,kA27,1,kA40,1,kA48,1,kA41,1,kA42,1,"
    "kA28,0,kA29,0,kA31,1,kA32,1,kA36,0,kA43,0,kA44,0,kA45,1,kA46,0,kA47,0,kA33,1,kA34,1,kA35,0,"
    "kA37,1,kA38,1,kA39,1,kA19,0,kA26,0,kA20,0,kA21,0,kA11,0"
)


def build_level_string(events, args, units_per_sec, pitch_center):
    speed_val = SPEED_ENUM[args.speed]
    header = HEADER_TEMPLATE.format(speed=speed_val)
    objs = [start_pos(START_X, START_Y, speed_val)]
    for t, note in events:
        x = args.start_x + t * units_per_sec
        pitch = fold_pitch(note, pitch_center, args.pitch_range)
        objs.append(sfx_trigger(x, TRIGGER_Y, pitch, args.sfx_id,
                                 args.volume, not args.no_reverb, args.sfx_duration))
    return header + ";" + ";".join(objs) + ";\n"


def gd_b64_gzip(level_string: str) -> str:
    comp = gzip.compress(level_string.encode('utf-8'), compresslevel=9)
    b64 = base64.b64encode(comp).decode('ascii')
    return b64.replace('+', '-').replace('/', '_')


def wrap_gmd(level_name: str, k4_string: str) -> str:
    return (
        '<?xml version="1.0"?><plist version="1.0" gjver="2.0"><dict>'
        '<k>kCEK</k><i>4</i>'
        f'<k>k2</k><s>{level_name}</s>'
        f'<k>k4</k><s>{k4_string}</s>'
        '<k>k5</k><s>esternex</s>'
        '<k>k101</k><s>0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0</s>'
        '<k>k13</k><t />'
        '<k>k21</k><i>2</i>'
        '<k>k16</k><i>1</i>'
        '<k>k80</k><i>21</i>'
        '<k>k50</k><i>47</i>'
        '<k>k47</k><t />'
        '<k>k48</k><i>1</i>'
        f'<k>k105</k><s>{"__parent_dict_placeholder__"}</s>'
        '</dict></plist>'
    ).replace('<k>k105</k><s>__parent_dict_placeholder__</s>',
              f'<k>k105</k><s>{592}</s>')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("midi_path", help="Input .mid file")
    p.add_argument("output_path", help="Output .gmd file to write")
    p.add_argument("--level-name", default="MIDI Import", help="Name stored in the .gmd (cosmetic)")

    p.add_argument("--sfx-id", type=int, default=592,
                    help="SFX asset id to use for every note (default: 20642, 'Piano Pop 1'). "
                         "Must already be downloaded in-game / referenced by the level.")
    p.add_argument("--sfx-duration", type=float, default=3.198,
                    help="Natural duration (s) of the chosen SFX asset, for the trigger's "
                         "informational Duration field (default matches 'Piano Pop 1').")
    p.add_argument("--volume", type=float, default=2.0, help="SFX trigger volume (default 2.0)")
    p.add_argument("--no-reverb", action="store_true", default=1, help="Disable the reverb flag (on by default)")

    p.add_argument("--speed", choices=SPEED_ENUM.keys(), default="fastest",
                    help="Level speed to design the level around (default: fastest/4x, "
                         "recommended for timing precision). This sets the Start Position's "
                         "speed AND the units/sec used to place triggers.")
    p.add_argument("--speed-scale", type=float, default=1.0,
                    help="Multiplier applied to the chosen speed's units/sec constant, in case "
                         "you need to calibrate for drift (e.g. 1.02 to speed triggers up 2%%).")
    p.add_argument("--start-x", type=float, default=30,
                    help="X position (units) where song time 0 begins (default 30, a small lead-in "
                         "after the spawn point)")

    p.add_argument("--pitch-range", type=int, default=12,
                    help="Max semitone offset the SFX trigger's Pitch will use in either direction "
                         "(GD's editor allows up to 12 = 2 octaves)")
    p.add_argument("--pitch-center", type=int, default=None,
                    help="MIDI note number to map to Pitch=0. Default: the median note of the song.")

    p.add_argument("--dedupe-ms", type=float, default=0,
                    help="Collapse notes starting within this many milliseconds of each other into "
                         "one trigger (keeping the highest note). 0 = keep every note (default).")

    p.add_argument("--max-seconds", type=float, default=None,
                    help="Only include notes up to this time — useful for generating a short "
                         "preview to test in-editor before committing to the full song.")

    args = p.parse_args()

    events = extract_notes(args.midi_path)
    if not events:
        sys.exit("No notes found in that MIDI file.")

    if args.max_seconds is not None:
        events = [e for e in events if e[0] <= args.max_seconds]

    events = dedupe_chords(events, args.dedupe_ms / 1000.0)

    pitch_center = args.pitch_center
    if pitch_center is None:
        pitch_center = round(statistics.median(n for _, n in events))

    units_per_sec = (SPEED_PRESETS_BLOCKS_PER_SEC[args.speed] * UNITS_PER_BLOCK
                      * args.speed_scale)

    level_string = build_level_string(events, args, units_per_sec, pitch_center)
    k4 = gd_b64_gzip(level_string)
    gmd = wrap_gmd(args.level_name, k4)

    with open(args.output_path, "w") as f:
        f.write(gmd)

    print(f"Wrote {args.output_path}")
    print(f"  notes -> triggers: {len(events)}")
    print(f"  pitch center (MIDI note): {pitch_center}  "
          f"(range in song: {min(n for _,n in events)}-{max(n for _,n in events)})")
    print(f"  speed: {args.speed} ({units_per_sec:.1f} units/sec)")
    print(f"  song duration used: {events[-1][0]:.2f}s -> final trigger x = "
          f"{args.start_x + events[-1][0]*units_per_sec:.1f}")


if __name__ == "__main__":
    main()
