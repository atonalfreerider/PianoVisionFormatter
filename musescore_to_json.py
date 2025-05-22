import xml.etree.ElementTree as ET
import json
import os
from typing import Dict, Any, List
from pv_util import format_output_filename, extract_metadata_from_musescore, extract_mscx_from_mscz, get_duration_ticks
from notes import Note, Track
from track_organizer import organize_tracks_v2

def _italian_tempo_to_bpm(text: str) -> int:
    """Map common Italian tempo words/abbreviations to a representative BPM."""
    if not text:
        return 0
    t = text.lower()
    # Common synonyms
    mapping = [
        (["larghissimo"], 24), (["grave"], 35), (["largo"], 45), (["larghetto"], 55),
        (["adagio"], 66), (["adagietto"], 72), (["andante"], 88), (["andantino"], 80),
        (["moderato"], 108), (["allegretto"], 112), (["allegro"], 132),
        (["vivace"], 156), (["vivacissimo"], 172), (["presto"], 184), (["prestissimo"], 208),
    ]
    for keys, bpm in mapping:
        if any(k in t for k in keys):
            return bpm
    return 0

def _text_tempo_to_bpm(text: str, last_bpm: int, first_bpm: int) -> int:
    """
    Interpret common textual tempo indications when no numeric 'tempo' is usable.
    Uses last/first bpm as reference.
    """
    if not text:
        return 0
    t = text.lower()
    # Explicit reset
    if "tempo i" in t or "tempo primo" in t or "tempo 1" in t:
        return first_bpm if first_bpm > 0 else (last_bpm if last_bpm > 0 else 120)
    # Broad categories: rallentando/ritardando/smorzando
    if "smorz" in t:
        return int(max(1, (last_bpm or 120) * 0.85))
    if "rall" in t or "rit" in t:
        if "poco" in t:
            return int(max(1, (last_bpm or 120) * 0.9))
        return int(max(1, (last_bpm or 120) * 0.8))
    if "string" in t or "stretto" in t:
        if "poco" in t:
            return int(max(1, (last_bpm or 120) * 1.1))
        return int(max(1, (last_bpm or 120) * 1.2))
    if "più mosso" in t or "piu mosso" in t:
        return int(max(1, (last_bpm or 120) * 1.15))
    if "meno mosso" in t:
        return int(max(1, (last_bpm or 120) * 0.85))
    # Italian base names as fallback
    bpm = _italian_tempo_to_bpm(text)
    return bpm if bpm > 0 else 0

def extract_tempo_changes(root: ET.Element) -> Dict[str, Any]:
    """Build a tempo timeline with tempo and pause events akin to MuseScore TempoMap."""
    division_elem = root.find(".//Division")
    resolution = int(division_elem.text) if division_elem is not None else 480

    # Measure starts/lengths based on first staff
    measure_starts = {}
    measure_lengths = {}
    current_tick = 0
    first_staff = None
    for staff in root.findall(".//Staff"):
        if int(staff.get('id', '1')) == 1:
            first_staff = staff
            break
    if first_staff is not None:
        for i, measure in enumerate(first_staff.findall(".//Measure")):
            measure_starts[i] = current_tick
            length = resolution * 4
            # Honor explicit len="x/y" first (pickup/irregular)
            mlen = measure.get("len")
            if mlen and "/" in mlen:
                try:
                    num, den = mlen.split("/")
                    length = int((int(num) / int(den)) * resolution * 4)
                except Exception:
                    pass
            # Compute time signature based length
            time_sig = measure.find(".//TimeSig")
            if time_sig is not None:
                try:
                    n = int(time_sig.find("sigN").text)
                    d = int(time_sig.find("sigD").text)
                    length = (resolution * 4 * n) // d
                except Exception:
                    pass
            irregular = measure.find(".//irregular")
            if irregular is not None:
                # pickup: use max voice span
                ml = 0
                for voice in measure.findall(".//voice"):
                    voice_len = 0
                    for elem in voice:
                        if elem.tag in ("Chord", "Rest"):
                            dur_e = elem.find("durationType")
                            if dur_e is not None:
                                voice_len += get_duration_ticks(dur_e.text, elem.findall("dots"), resolution)
                    ml = max(ml, voice_len)
                if ml > 0:
                    length = ml
            measure_lengths[i] = length
            current_tick += length

    # Collect explicit tempos and fermata pauses (beats) scanning all voices of staff 1
    timeline_events = []  # [{'type':'tempo','ticks', 'bpm', 'time'}, {'type':'pause','ticks','beats','time'}]
    explicit_tempos = []
    fermata_pauses = []  # store beats (duration in beats, tempo-independent)

    def add_tempo_event(ticks: int, bpm: int):
        if bpm <= 0:
            return
        explicit_tempos.append({"bpm": int(round(bpm)), "ticks": ticks, "time": 0.0})

    def add_fermata_pause(end_tick: int, beats: float = 0.5):
        # 0.5 beats default pause
        fermata_pauses.append({"ticks": end_tick, "beats": beats, "time": 0.0})

    # NEW: seed first_bpm_seen and helper for text-only tempos
    first_bpm_seen = None
    def current_last_bpm() -> int:
        return explicit_tempos[-1]["bpm"] if explicit_tempos else (first_bpm_seen or 120)

    for staff in root.findall(".//Staff"):
        if int(staff.get('id', '1')) != 1:
            continue
        for i, measure in enumerate(staff.findall(".//Measure")):
            measure_tick = measure_starts.get(i, i * resolution * 4)
            # Per voice walk to get precise offsets and detect fermatas on notes/rests
            for voice in measure.findall("voice"):
                voice_tick = measure_tick
                for elem in voice:
                    tag = elem.tag
                    if tag == "Tempo":
                        # Numeric tempo
                        tempo_elem = elem.find("tempo")
                        text_elem = elem.find("text")
                        bpm = 0
                        if tempo_elem is not None and tempo_elem.text:
                            try:
                                bpm = round(float(tempo_elem.text) * 60.0)
                            except Exception:
                                bpm = 0
                        if bpm == 0 and text_elem is not None and text_elem.text:
                            # Try text-based tempo using last/first
                            bpm = _text_tempo_to_bpm(text_elem.text, current_last_bpm(), first_bpm_seen or 0)
                        if bpm > 0:
                            add_tempo_event(voice_tick, bpm)
                            if first_bpm_seen is None:
                                first_bpm_seen = bpm
                    if tag == "Chord":
                        # articulation fermata?
                        has_fermata = False
                        for art in elem.findall(".//Articulation"):
                            st = art.find("subtype")
                            if st is not None and st.text and "fermata" in st.text.lower():
                                has_fermata = True
                                break
                        dur_e = elem.find("durationType")
                        if dur_e is not None:
                            dur = get_duration_ticks(dur_e.text, elem.findall("dots"), resolution)
                            # accumulate tick
                            # handle nested chord-continuations: we always advance by chord duration, chords in same time share same start_tick
                            if has_fermata:
                                add_fermata_pause(voice_tick + dur, 0.5)
                            voice_tick += dur
                    elif tag == "Rest":
                        # fermata on rest?
                        has_fermata = False
                        for art in elem.findall(".//Articulation"):
                            st = art.find("subtype")
                            if st is not None and st.text and "fermata" in st.text.lower():
                                has_fermata = True
                                break
                        dur_e = elem.find("durationType")
                        if dur_e is not None:
                            dur = get_duration_ticks(dur_e.text, elem.findall("dots"), resolution)
                            if has_fermata:
                                add_fermata_pause(voice_tick + dur, 0.5)
                            voice_tick += dur

            # Also catch Tempo under measure scope
            for t in measure.findall(".//Tempo"):
                tempo_elem = t.find("tempo")
                text_elem = t.find("text")
                bpm = 0
                if tempo_elem is not None and tempo_elem.text:
                    try:
                        bpm = round(float(tempo_elem.text) * 60.0)
                    except Exception:
                        bpm = 0
                if bpm == 0 and text_elem is not None and text_elem.text:
                    bpm = _text_tempo_to_bpm(text_elem.text, current_last_bpm(), first_bpm_seen or 0)
                if bpm > 0:
                    add_tempo_event(measure_tick, bpm)
                    if first_bpm_seen is None:
                        first_bpm_seen = bpm

    # Default tempo at start if none present
    if not explicit_tempos:
        explicit_tempos.append({"bpm": 120, "ticks": 0, "time": 0.0})
    else:
        # Ensure a base tempo at 0 for correct integration
        if explicit_tempos[0]["ticks"] != 0:
            explicit_tempos.insert(0, {"bpm": explicit_tempos[0]["bpm"], "ticks": 0, "time": 0.0})

    # Gradual tempo changes (rit./accel. spanners)
    gradual_tempos = []
    for spanner in root.findall(".//Spanner[@type='GradualTempoChange']"):
        gtc = spanner.find("GradualTempoChange")
        if gtc is None:
            continue
        change_type = gtc.find("tempoChangeType")
        if change_type is None or not change_type.text:
            continue
        # Determine measure range
        start_measure = 0
        end_measure = 1
        segments = gtc.findall("Segment")
        if segments:
            for segment in segments:
                m = segment.get("measure")
                if m is not None:
                    try:
                        mn = int(m)
                        start_measure = min(start_measure, mn)
                        end_measure = max(end_measure, mn + 1)
                    except Exception:
                        pass
        start_tick = measure_starts.get(start_measure, start_measure * resolution * 4)
        end_tick = measure_starts.get(end_measure, end_measure * resolution * 4)
        if end_tick <= start_tick:
            end_tick = start_tick + resolution * 4
        change_factors = {
            "ritardando": 0.75, "rallentando": 0.75, "rall.": 0.75, "rit.": 0.75,
            "accelerando": 1.33, "accel.": 1.33, "accel": 1.33
        }
        factor = change_factors.get(change_type.text.lower(), 1.0)
        # Find current tempo at start
        current = 120
        for t in reversed(explicit_tempos):
            if t["ticks"] <= start_tick:
                current = t["bpm"]
                break
        end_tempo = int(current * factor)
        span = end_tick - start_tick
        if span > 0:
            num_points = min(max(8, span // (resolution // 2)), 16)
            for i in range(num_points):
                progress = (i + 1) / num_points
                point_tick = start_tick + int(progress * span)
                if factor < 1.0:
                    curve = 1 - (1 - progress) ** 2
                else:
                    curve = progress ** 2
                point_bpm = int(current + (end_tempo - current) * curve)
                gradual_tempos.append({"bpm": point_bpm, "ticks": point_tick, "time": 0.0})

    # Normalize timeline like TempoMap::normalize
    events = [{"type": "tempo", "ticks": e["ticks"], "bpm": e["bpm"], "time": 0.0} for e in (explicit_tempos + gradual_tempos)]
    events += [{"type": "pause", "ticks": p["ticks"], "beats": p["beats"], "time": 0.0} for p in fermata_pauses]
    # Sort by tick then tempo before pause (so pauses use the tempo valid at that tick)
    events.sort(key=lambda e: (e["ticks"], 0 if e["type"] == "tempo" else 1))

    last_time = 0.0
    last_tick = 0
    last_bpm = 120 if not explicit_tempos else explicit_tempos[0]["bpm"]

    for ev in events:
        if ev["ticks"] != last_tick:
            delta_ticks = ev["ticks"] - last_tick
            last_time += (delta_ticks * 60.0) / (last_bpm * resolution)
            last_tick = ev["ticks"]
        if ev["type"] == "tempo":
            ev["time"] = last_time
            last_bpm = ev["bpm"]
        else:  # pause
            # Convert beats to seconds using current bpm at this tick
            pause_secs = (ev["beats"] * 60.0) / last_bpm
            last_time += pause_secs
            ev["time"] = last_time

    # Build final outputs: tempos for JSON, full timeline for conversion
    tempo_events = [ {"bpm": ev["bpm"], "ticks": ev["ticks"], "time": ev["time"]} for ev in events if ev["type"] == "tempo" ]
    return {"tempos": sorted(tempo_events, key=lambda x: x["ticks"]), "timeline": events, "resolution": resolution}

def ticks_to_seconds(ticks: int, timeline_events: List[Dict[str, Any]], resolution: int) -> float:
    """Convert ticks to seconds using normalized timeline with tempo and pause events."""
    if not timeline_events:
        return (ticks * 60.0) / (120 * resolution)
    # Find last event at or before ticks and last tempo at or before ticks
    last_event = timeline_events[0]
    last_tempo_bpm = 120
    for ev in timeline_events:
        if ev["type"] == "tempo":
            if ev["ticks"] <= ticks:
                last_tempo_bpm = ev["bpm"]
        if ev["ticks"] <= ticks:
            last_event = ev
        else:
            break
    if last_event["ticks"] == ticks:
        # If the event is a pause, its time already includes the pause
        return last_event["time"]
    base_time = last_event["time"]
    base_ticks = last_event["ticks"]
    delta_ticks = ticks - base_ticks
    return base_time + (delta_ticks * 60.0) / (last_tempo_bpm * resolution)

def create_measure_ticks_map(score: ET.Element, resolution: int) -> Dict[int, Dict[int, int]]:
    """Create measure tick mapping using MuseScore's internal timing"""
    staff_measure_ticks = {}
    measure_lengths = {}
    
    # Calculate measure lengths from first staff
    first_staff = None
    all_staves = score.findall(".//Staff")
    
    # Debug: print staff structure
    print(f"Found {len(all_staves)} staves in score")
    
    for staff in all_staves:
        staff_id = int(staff.get('id', '1'))
        if staff_id == 1:
            first_staff = staff
            break
    
    if first_staff is None:
        print("Warning: No first staff found, using any available staff")
        if all_staves:
            first_staff = all_staves[0]
        else:
            return {}
    
    current_tick = 0
    measures = first_staff.findall("Measure")
    
    # Debug: print measure structure
    print(f"Found {len(measures)} measures in first staff")
    
    for measure_idx, measure in enumerate(measures):
        measure_lengths[measure_idx] = resolution * 4  # Default 4/4
        # Prefer explicit len="x/y" for pickups/irregular measures
        mlen = measure.get("len")
        if mlen and "/" in mlen:
            try:
                num, den = mlen.split("/")
                measure_lengths[measure_idx] = int((int(num) / int(den)) * resolution * 4)
            except Exception:
                pass
        
        # Check for time signature
        time_sig = measure.find(".//TimeSig")
        if time_sig is not None:
            try:
                numerator = int(time_sig.find("sigN").text)
                denominator = int(time_sig.find("sigD").text)
                measure_lengths[measure_idx] = (resolution * 4 * numerator) // denominator
            except (AttributeError, TypeError, ValueError):
                pass
        
        # Handle pickup measures
        irregular = measure.find(".//irregular")
        if irregular is not None:
            measure_length = 0
            for voice in measure.findall(".//voice"):
                voice_length = 0
                for elem in voice:
                    if elem.tag in ["Chord", "Rest"]:
                        duration_type = elem.find("durationType")
                        if duration_type is not None:
                            duration_ticks = get_duration_ticks(duration_type.text, elem.findall("dots"), resolution)
                            voice_length += duration_ticks
                measure_length = max(measure_length, voice_length)
            if measure_length > 0:
                measure_lengths[measure_idx] = measure_length
    
    # Calculate absolute positions for each staff
    for staff in all_staves:
        staff_id = int(staff.get('id', '1'))
        staff_measure_ticks[staff_id] = {}
        
        current_tick = 0
        measures = staff.findall("Measure")
        
        for measure_idx, measure in enumerate(measures):
            staff_measure_ticks[staff_id][measure_idx] = current_tick
            current_tick += measure_lengths.get(measure_idx, resolution * 4)
    
    return staff_measure_ticks

def _extract_note_velocity(note_elem):
    velocity_elem = note_elem.find("velocity")
    try:
        if velocity_elem is not None and velocity_elem.text:
            v = float(velocity_elem.text)
            # MuseScore stores raw 0-127; normalize (fallback 0.8)
            return max(0.05, min(1.0, v / 127.0))
    except Exception:
        pass
    return 0.8

def _detect_tie_flags(note_elem):
    """
    Detect tie start/stop using Spanner[@type='Tie'] prev/next, with legacy <tie>/<Tie> fallback.
    """
    # Prefer spanner-based detection (robust in MuseScore 4)
    tie_start = False
    tie_stop = False
    for sp in note_elem.findall(".//Spanner[@type='Tie']"):
        has_prev = sp.find("prev") is not None
        has_next = sp.find("next") is not None
        tie_start = tie_start or has_next
        tie_stop = tie_stop or has_prev
    if tie_start or tie_stop:
        return tie_start, tie_stop

    # Fallback: legacy inline <tie>/<Tie> with optional type attributes
    tie_elems = []
    tie_elems.extend(note_elem.findall("tie"))
    tie_elems.extend(note_elem.findall("Tie"))
    if not tie_elems:
        return False, False

    def is_start(te):
        t = (te.get("type") or "").lower()
        if t == "start":
            return True
        txt = (te.text or "").strip().lower()
        return "start" in txt if txt else False

    def is_stop(te):
        t = (te.get("type") or "").lower()
        if t == "stop":
            return True
        txt = (te.text or "").strip().lower()
        return "stop" in txt if txt else False

    tie_start = any(is_start(te) or (te.get("type") in (None, "")) for te in tie_elems)
    tie_stop = any(is_stop(te) for te in tie_elems)
    return tie_start, tie_stop

def _collect_voice_events(measures, staff_id, staff_measure_ticks, resolution):
    """
    Build per-voice chronological list of chord/rest events.
    """
    voice_events = {}
    for measure_idx, measure in enumerate(measures):
        if staff_id in staff_measure_ticks and measure_idx in staff_measure_ticks[staff_id]:
            measure_start_ticks = staff_measure_ticks[staff_id][measure_idx]
        else:
            measure_start_ticks = measure_idx * resolution * 4
        for voice_idx, voice in enumerate(measure.findall("voice")):
            current_tick = measure_start_ticks
            active_tuplet = None
            if voice_idx not in voice_events:
                voice_events[voice_idx] = []
            for elem in voice:
                tag = elem.tag
                if tag == "Tuplet":
                    normal_notes = elem.find("normalNotes")
                    actual_notes = elem.find("actualNotes")
                    base_note = elem.find("baseNote")
                    if normal_notes is not None and actual_notes is not None and base_note is not None:
                        try:
                            normal = int(normal_notes.text)
                            actual = int(actual_notes.text)
                            if actual > 0:
                                active_tuplet = {
                                    "ratio": normal / actual
                                }
                        except Exception:
                            active_tuplet = None
                elif tag == "endTuplet":
                    active_tuplet = None
                elif tag == "location":
                    # Do not alter current time; layout anchor for spanners, not time progression
                    continue
                elif tag == "Rest":
                    dur_type = elem.find("durationType")
                    if dur_type is not None and dur_type.text:
                        base = get_duration_ticks(dur_type.text, elem.findall("dots"), resolution)
                        if active_tuplet:
                            base = int(base * active_tuplet["ratio"])
                        current_tick += base
                elif tag == "Chord":
                    duration_elem = elem.find("durationType")
                    if duration_elem is None or not duration_elem.text:
                        continue
                    base_duration = get_duration_ticks(duration_elem.text, elem.findall("dots"), resolution)
                    if active_tuplet:
                        base_duration = int(base_duration * active_tuplet["ratio"])

                    # Accent / marcato detection
                    has_accent = False
                    for articulation in elem.findall(".//Articulation"):
                        subtype = articulation.find("subtype")
                        if subtype is not None and subtype.text:
                            st = subtype.text.lower()
                            if "accent" in st or "marcato" in st or "sforzato" in st:
                                has_accent = True
                                break

                    note_list = []
                    for note_elem in elem.findall("Note"):
                        pitch_elem = note_elem.find("pitch")
                        if pitch_elem is None or not pitch_elem.text:
                            continue
                        try:
                            pitch = int(pitch_elem.text)
                        except Exception:
                            continue
                        velocity = _extract_note_velocity(note_elem)
                        if has_accent and velocity < 0.9:
                            velocity = min(1.0, velocity * 1.25)
                        tie_start, tie_stop = _detect_tie_flags(note_elem)
                        note_list.append({
                            "pitch": pitch,
                            "velocity": velocity,
                            "accent": 1 if has_accent else 0,
                            "tie_start": tie_start,
                            "tie_stop": tie_stop,
                        })

                    voice_events[voice_idx].append({
                        "start_tick": current_tick,
                        "duration": base_duration,
                        "notes": note_list,
                        "measure_idx": measure_idx
                    })

                    # Always advance for each Chord (prevents overlap)
                    current_tick += base_duration
    return voice_events

def _render_voice_events_with_ties(events, staff_hand, timeline, resolution):
    """
    Stream-render a voice with proper tie merging.
    """
    from notes import Note  # local import
    out = []
    active = {}  # pitch -> {start_tick, duration, velocity, accent, measure_idx}
    events = sorted(events, key=lambda e: e["start_tick"])

    for ev in events:
        start_tick = ev["start_tick"]
        dur = ev["duration"]
        meas = ev["measure_idx"]

        for nd in ev["notes"]:
            pitch = nd["pitch"]
            v = nd["velocity"]
            acc = nd.get("accent", 0)
            tie_start = bool(nd.get("tie_start"))
            tie_stop = bool(nd.get("tie_stop"))

            # Start or continue a chain (start-only)
            if tie_start and not tie_stop:
                if pitch not in active:
                    active[pitch] = {
                        "start_tick": start_tick,
                        "duration": 0,
                        "velocity": v,
                        "accent": acc,
                        "measure_idx": meas
                    }
                active[pitch]["duration"] += dur
                continue

            # Middle link (both prev and next): extend but do NOT finalize
            if tie_start and tie_stop:
                if pitch not in active:
                    active[pitch] = {
                        "start_tick": start_tick,
                        "duration": 0,
                        "velocity": v,
                        "accent": acc,
                        "measure_idx": meas
                    }
                active[pitch]["duration"] += dur
                continue

            # Final link (stop-only): extend then finalize
            if tie_stop and not tie_start:
                if pitch in active:
                    active[pitch]["duration"] += dur
                    s_tick = active[pitch]["start_tick"]
                    d_ticks = active[pitch]["duration"]
                    s_time = ticks_to_seconds(s_tick, timeline, resolution)
                    e_time = ticks_to_seconds(s_tick + d_ticks, timeline, resolution)
                    out.append(Note(
                        midi=pitch,
                        time=s_time,
                        velocity=active[pitch]["velocity"],
                        duration=e_time - s_time,
                        ticks=s_tick,
                        duration_ticks=d_ticks,
                        staff=staff_hand,
                        group=active[pitch]["measure_idx"],
                        accent=active[pitch]["accent"],
                    ))
                    del active[pitch]
                else:
                    # Stop without active start -> standalone
                    s_time = ticks_to_seconds(start_tick, timeline, resolution)
                    e_time = ticks_to_seconds(start_tick + dur, timeline, resolution)
                    out.append(Note(
                        midi=pitch,
                        time=s_time,
                        velocity=v,
                        duration=e_time - s_time,
                        ticks=start_tick,
                        duration_ticks=dur,
                        staff=staff_hand,
                        group=meas,
                        accent=acc,
                    ))
                continue

            # No ties: standalone
            s_time = ticks_to_seconds(start_tick, timeline, resolution)
            e_time = ticks_to_seconds(start_tick + dur, timeline, resolution)
            out.append(Note(
                midi=pitch,
                time=s_time,
                velocity=v,
                duration=e_time - s_time,
                ticks=start_tick,
                duration_ticks=dur,
                staff=staff_hand,
                group=meas,
                accent=acc,
            ))

    # Flush unterminated chains (rare; keep safety)
    for pitch, info in active.items():
        s_tick = info["start_tick"]
        d_ticks = info["duration"]
        s_time = ticks_to_seconds(s_tick, timeline, resolution)
        e_time = ticks_to_seconds(s_tick + d_ticks, timeline, resolution)
        out.append(Note(
            midi=pitch,
            time=s_time,
            velocity=info["velocity"],
            duration=e_time - s_time,
            ticks=s_tick,
            duration_ticks=d_ticks,
            staff=staff_hand,
            group=info["measure_idx"],
            accent=info["accent"],
        ))
    return out

def _dedup_notes(notes):
    """
    Deduplicate exact duplicates: keep the longest duration per (staff, ticks, midi).
    """
    by_key = {}
    for n in notes:
        key = (n.staff, n.ticks, n.midi)
        if key not in by_key or n.duration_ticks > by_key[key].duration_ticks:
            by_key[key] = n
    return list(by_key.values())

def _finalize_notes_from_events(voice_events, staff_hand, timeline, resolution):
    """Render each voice with tie handling and dedup."""
    all_notes = []
    for _, evs in voice_events.items():
        all_notes.extend(_render_voice_events_with_ties(evs, staff_hand, timeline, resolution))
    return _dedup_notes(all_notes)
def parse_musescore(mscx_content: str, mscz_path: str) -> Dict[str, Any]:
    """Parse MuseScore file and convert to Piano Vision format.
    Relies on companion MIDI for tempo & measure accuracy if present.
    """
    root = ET.fromstring(mscx_content)
    
    # Extract metadata
    title, artist, _ = extract_metadata_from_musescore(root, mscz_path)  # previously ignored third value
    division_elem = root.find(".//Division")
    resolution = int(division_elem.text) if division_elem is not None else 480
    
    # Find the Score element - MuseScore files have the root as the score
    # The actual staff data is directly under the root or under a Score child
    score = root.find("Score")
    if score is None:
        # If no Score child, the root itself contains the score data
        score = root
    
    # Verify we have staff data
    staves = score.findall(".//Staff")
    if not staves:
        # Try alternative path - sometimes staves are nested differently
        staves = root.findall(".//Staff")
        if not staves:
            raise ValueError("No Staff elements found in MuseScore file")
        # Use root as score if we found staves there
        score = root

    # Build tempo timeline (tempos for output, timeline for conversion)
    tempo_map = extract_tempo_changes(root)
    tempos = tempo_map["tempos"]
    timeline = tempo_map["timeline"]
    resolution = tempo_map.get("resolution", resolution)

    # Create tick mapping for measures across staves
    staff_measure_ticks = create_measure_ticks_map(score, resolution)

    # Initialize tracking variables
    measure_count = 0
    time_signatures = []
    key_signatures = []
    
    # Map staves to hands - explicitly assign staff 1 to right hand, all others to left hand
    staff_map = {}
    staves = score.findall(".//Staff")
    
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        staff_map[staff_id] = 1 if staff_id == 1 else 2
    
    # First pass: process time signatures and key signatures
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        if staff_id == 1:  # Only process first staff for signatures
            measures = staff.findall("Measure")
            measure_count = max(measure_count, len(measures))
            
            for measure_idx, measure in enumerate(measures):
                # Get the absolute tick position for this measure
                if staff_id in staff_measure_ticks and measure_idx in staff_measure_ticks[staff_id]:
                    measure_start_ticks = staff_measure_ticks[staff_id][measure_idx]
                else:
                    measure_start_ticks = measure_idx * resolution * 4
                
                # Process time signature
                time_sig = measure.find(".//TimeSig")
                if time_sig is not None:
                    try:
                        numerator = int(time_sig.find("sigN").text)
                        denominator = int(time_sig.find("sigD").text)
                        time_signatures.append({
                            "ticks": measure_start_ticks,
                            "timeSignature": [str(numerator), str(denominator)],
                            "measures": str(measure_idx)
                        })
                    except (AttributeError, TypeError):
                        pass
                
                # Process key signature
                key_sig = measure.find(".//KeySig")
                if key_sig is not None:
                    key = key_sig.find("concertKey")
                    if key is not None:
                        key_value = int(key.text)
                        key_map = {0: "C", 1: "G", 2: "D", -1: "F", -2: "Bb"}
                        key_signatures.append({
                            "ticks": measure_start_ticks,
                            "key": key_map.get(key_value, "C"),
                            "scale": "major"
                        })
    
    # COMPLETE REWRITE OF NOTE COLLECTION LOGIC (refined & tie-aware)
    all_notes = []
    staves = score.findall(".//Staff")
    staff_map = {}
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        staff_map[staff_id] = 1 if staff_id == 1 else 2

    # Build per-staff events then convert
    for staff in staves:
        staff_id = int(staff.get('id', '0'))
        staff_hand = staff_map.get(staff_id, 2)
        measures = staff.findall("Measure")
        voice_events = _collect_voice_events(measures, staff_id, staff_measure_ticks, resolution)
        # Convert to Note objects with tie merging
        staff_notes = _finalize_notes_from_events(voice_events, staff_hand, timeline, resolution)
        all_notes.extend(staff_notes)

    # Split hands
    right_hand_notes = [n for n in all_notes if n.staff == 1]
    left_hand_notes = [n for n in all_notes if n.staff == 2]

    # Sort by ticks then midi
    right_hand_notes.sort(key=lambda x: (x.ticks, x.midi))
    left_hand_notes.sort(key=lambda x: (x.ticks, x.midi))

    # Ensure at least one time signature
    if not time_signatures:
        time_signatures.append({
            "ticks": 0,
            "timeSignature": ["4", "4"],
            "measures": "0"
        })
    
    # Build measures with accurate start times via timeline
    measure_ticks = []
    for i in range(measure_count):
        if 1 in staff_measure_ticks and i in staff_measure_ticks[1]:
            measure_start_ticks = staff_measure_ticks[1][i]
        else:
            measure_start_ticks = i * resolution * 4
        current_time_sig = next((ts for ts in reversed(time_signatures) if float(ts["measures"]) <= i), time_signatures[0])
        numerator = int(current_time_sig["timeSignature"][0])
        denominator = int(current_time_sig["timeSignature"][1])
        measure_length = (resolution * 4 * numerator) // denominator
        measure_start_time = ticks_to_seconds(measure_start_ticks, timeline, resolution)
        measure_ticks.append({
            "time": measure_start_time,
            "timeSignature": current_time_sig["timeSignature"],
            "ticksPerMeasure": measure_length,
            "ticksStart": measure_start_ticks,
            "totalTicks": measure_length,
            "type": "0.000" if i == 0 else "2"
        })

    # Create tracks with properly sorted notes
    right_track = Track(
        notes=right_hand_notes,
        myInstrument=-5, 
        theirInstrument=0
    )
    
    left_track = Track(
        notes=left_hand_notes, 
        myInstrument=-5, 
        theirInstrument=0
    )
    
    # Calculate song length
    song_length = 0
    if right_hand_notes or left_hand_notes:
        all_notes = right_hand_notes + left_hand_notes
        song_length = max([note.time + note.duration for note in all_notes]) if all_notes else 0
    
    # Create final output with all notes and their precise timing
    return {
        "supportingTracks": [
            {
                "notes": [
                    {
                        "midi": note.midi,
                        "time": note.time,
                        "velocity": note.velocity,
                        "duration": note.duration
                    }
                    for note in track.notes
                ],
                "myInstrument": track.myInstrument,
                "theirInstrument": track.theirInstrument
            }
            for track in [right_track, left_track]
        ],
        "start_time": 0,
        "song_length": song_length,
        "resolution": resolution,
        "tempos": tempos,
        "keySignatures": key_signatures,
        "timeSignatures": time_signatures,
        "measures": measure_ticks,
        "tracksV2": organize_tracks_v2([right_track, left_track], measure_ticks, tempos, resolution),
        "accompanyingInstruments": [-2, -1],
        "accompanyingChannels": [0, 0],
        "name": title,
        "artist": artist,
        "accompanyingTracks": []
    }

def main():
    import sys
    
    # Handle command line arguments with optional output dir and feature flags
    if len(sys.argv) < 2:
        print("Usage: python musescore_to_json.py <input_file> [output_dir] [orchestra_mode] [simplified_mode]")
        sys.exit(1)

    mscz_path = sys.argv[1]
    
    # If no output directory is specified, use the same directory as the input file
    if len(sys.argv) >= 3:
        output_dir = sys.argv[2]
    else:
        output_dir = os.path.dirname(mscz_path)
        
    # Get optional flags with defaults
    orchestra_mode = False
    simplified_mode = False
    
    if len(sys.argv) > 3:
        orchestra_mode = sys.argv[3].lower() == "true"
    if len(sys.argv) > 4:
        simplified_mode = sys.argv[4].lower() == "true"

    if not os.path.isfile(mscz_path):
        print(f"Error: {mscz_path} is not a file")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Extract .mscx content from .mscz
        mscx_content = extract_mscx_from_mscz(mscz_path)
        if not mscx_content:
            raise Exception("Failed to extract MSCX content from MSCZ file")
        
        # Parse the content
        output_json = parse_musescore(mscx_content, mscz_path)
        
        # Generate output filename
        output_filename = format_output_filename(
            output_json['name'],
            output_json['artist'],
            mscz_path
        )
        
        # Write output file
        output_path = os.path.join(output_dir, output_filename)
        with open(output_path, 'w') as f:
            json.dump(output_json, f)
        
        print(f"Converted: {mscz_path} -> {output_path}")
    except Exception as e:
        print(f"Error processing {mscz_path}: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
