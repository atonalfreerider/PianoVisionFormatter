import json
import sys
import os
import matplotlib.pyplot as plt
import numpy as np
from typing import Dict, List, Any, Tuple

def load_json(file_path: str) -> Dict[str, Any]:
    """Load JSON file and return as dictionary"""
    with open(file_path, 'r') as f:
        return json.load(f)

def tempo_at_time(tempos: List[Dict[str, Any]], target_time: float) -> float:
    """Return the BPM at a specific time point"""
    # Sort tempos by time
    sorted_tempos = sorted(tempos, key=lambda x: float(x["time"]))
    
    # If no tempos or target time is before first tempo, return default
    if not sorted_tempos or target_time < float(sorted_tempos[0]["time"]):
        return 45  # Default tempo
    
    # Find the last tempo that's less than or equal to our target time
    for i in range(len(sorted_tempos) - 1):
        if float(sorted_tempos[i]["time"]) <= target_time < float(sorted_tempos[i+1]["time"]):
            return float(sorted_tempos[i]["bpm"])
    
    # If we're past all tempo markings, return the last tempo
    return float(sorted_tempos[-1]["bpm"])

def interpolate_tempo_at_tick(tempos: List[Dict[str, Any]], target_tick: int) -> float:
    """Return the interpolated BPM at a specific tick position"""
    # Sort tempos by ticks
    sorted_tempos = sorted(tempos, key=lambda x: int(x["ticks"]))
    
    # If no tempos or target tick is before first tempo, return default
    if not sorted_tempos or target_tick < int(sorted_tempos[0]["ticks"]):
        return 45  # Default tempo
    
    # Find the last tempo that's less than or equal to our target tick
    for i in range(len(sorted_tempos) - 1):
        current = int(sorted_tempos[i]["ticks"])
        next_tick = int(sorted_tempos[i+1]["ticks"])
        
        if current <= target_tick < next_tick:
            # Simple linear interpolation between tempo points
            current_bpm = float(sorted_tempos[i]["bpm"])
            next_bpm = float(sorted_tempos[i+1]["bpm"])
            
            # Calculate position ratio between tempo points
            ratio = (target_tick - current) / (next_tick - current) if (next_tick - current) > 0 else 0
            
            # Interpolate BPM
            return current_bpm + ratio * (next_bpm - current_bpm)
    
    # If we're past all tempo markings, return the last tempo
    return float(sorted_tempos[-1]["bpm"])

def collect_time_points(ref_tempos: List[Dict[str, Any]], gen_tempos: List[Dict[str, Any]]) -> List[float]:
    """Collect all unique time points from both tempo lists for comparison"""
    time_points = set()
    
    # Add all time points from both tempo lists
    for tempo in ref_tempos:
        time_points.add(float(tempo["time"]))
    for tempo in gen_tempos:
        time_points.add(float(tempo["time"]))
        
    # Sort time points
    return sorted(time_points)

def compare_tempos(generated_path: str, reference_path: str) -> List[Dict[str, Any]]:
    """Compare tempos between generated and reference files at all reference time points"""
    # Load files
    generated = load_json(generated_path)
    reference = load_json(reference_path)
    
    # Extract tempo lists
    gen_tempos = generated.get("tempos", [])
    ref_tempos = reference.get("tempos", [])
    
    # Use reference time points for comparison
    ref_time_points = [float(tempo["time"]) for tempo in ref_tempos]
    ref_time_points = sorted(list(set(ref_time_points)))  # Remove duplicates and sort
    
    # Compare tempos at each reference time point
    results = []
    for time_point in ref_time_points:
        gen_bpm = tempo_at_time(gen_tempos, time_point)
        ref_bpm = tempo_at_time(ref_tempos, time_point)
        
        # Record comparison result
        results.append({
            "time": time_point,
            "generated_bpm": gen_bpm,
            "reference_bpm": ref_bpm,
            "diff": abs(gen_bpm - ref_bpm),
            "match": abs(gen_bpm - ref_bpm) <= 5  # Allow 5 BPM difference as acceptable
        })
    
    return results

def plot_tempo_comparison(generated_path: str, reference_path: str, output_path: str = None) -> None:
    """Create a plot comparing tempos between generated and reference files"""
    # Load files
    generated = load_json(generated_path)
    reference = load_json(reference_path)
    
    # Extract tempo lists
    gen_tempos = generated.get("tempos", [])
    ref_tempos = reference.get("tempos", [])
    
    # Get all time points for a smooth graph
    time_points = collect_time_points(ref_tempos, gen_tempos)
    
    # Create interpolated tempo values for both files
    gen_times = [float(t["time"]) for t in gen_tempos]
    gen_bpms = [float(t["bpm"]) for t in gen_tempos]
    
    ref_times = [float(t["time"]) for t in ref_tempos]
    ref_bpms = [float(t["bpm"]) for t in ref_tempos]
    
    # Create plot
    plt.figure(figsize=(14, 8))
    
    # Plot tempo lines
    plt.plot(gen_times, gen_bpms, 'b-', label='Generated', marker='o', linewidth=1, markersize=3)
    plt.plot(ref_times, ref_bpms, 'r-', label='Reference', marker='x', linewidth=1, markersize=3)
    
    # Add labels for significant differences
    for g_time, g_bpm in zip(gen_times, gen_bpms):
        r_bpm = tempo_at_time(ref_tempos, g_time)
        if abs(g_bpm - r_bpm) > 10:
            plt.annotate(f"{int(g_bpm)} vs {int(r_bpm)}", 
                        xy=(g_time, g_bpm), 
                        xytext=(0, 10),
                        textcoords='offset points',
                        fontsize=8,
                        arrowprops=dict(arrowstyle='->', color='gray'))
    
    # Add grid and labels
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.title(f'Tempo Comparison\n{os.path.basename(generated_path)} vs {os.path.basename(reference_path)}')
    plt.xlabel('Time (seconds)')
    plt.ylabel('Tempo (BPM)')
    plt.legend()
    
    # Set reasonable y-axis limits
    all_bpms = gen_bpms + ref_bpms
    plt.ylim(max(15, min(all_bpms) - 10), max(all_bpms) + 10)
    
    # Save or show the plot
    if output_path:
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Tempo comparison plot saved to: {output_path}")
    else:
        plt.show()

def analyze_tempo_differences(results: List[Dict[str, Any]], threshold: float = 5.0) -> Tuple[float, List[Dict[str, Any]]]:
    """Analyze tempo comparison results and return overall match score and problematic points"""
    if not results:
        return 0.0, []
    
    # Count matches
    matches = sum(1 for r in results if r["match"])
    match_percentage = (matches / len(results)) * 100
    
    # Find problematic points (with differences above threshold)
    problems = [r for r in results if r["diff"] > threshold]
    problems.sort(key=lambda x: x["diff"], reverse=True)
    
    return match_percentage, problems

def main():
    if len(sys.argv) < 3:
        print("Usage: python tempo_validator.py <generated_json> <reference_json> [--plot]")
        sys.exit(1)
    
    generated_path = sys.argv[1]
    reference_path = sys.argv[2]
    
    # Check if files exist
    if not os.path.exists(generated_path):
        print(f"Error: {generated_path} does not exist")
        sys.exit(1)
    if not os.path.exists(reference_path):
        print(f"Error: {reference_path} does not exist")
        sys.exit(1)
    
    # Compare tempos
    results = compare_tempos(generated_path, reference_path)
    match_score, problems = analyze_tempo_differences(results)
    
    # Print results
    print(f"Tempo Analysis Results:")
    print(f"  - Match Score: {match_score:.1f}%")
    print(f"  - Total Time Points: {len(results)}")
    print(f"  - Matching Points: {sum(1 for r in results if r['match'])}")
    print(f"  - Problem Points: {len(problems)}")
    
    # Print top problems
    if problems:
        print("\nTop Problems:")
        for i, prob in enumerate(problems[:10]):  # Show top 10 problems
            print(f"  {i+1}. Time: {prob['time']}s - Generated: {prob['generated_bpm']}bpm, Reference: {prob['reference_bpm']}bpm (Diff: {prob['diff']:.1f})")
    
    # Create plot if requested
    if "--plot" in sys.argv:
        output_path = os.path.splitext(generated_path)[0] + '_tempo_comparison.png'
        plot_tempo_comparison(generated_path, reference_path, output_path)

if __name__ == "__main__":
    main()
