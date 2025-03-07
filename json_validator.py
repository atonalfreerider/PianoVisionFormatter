import json
from typing import Dict, Any, List
import sys
from deepdiff import DeepDiff
import os

def load_json(file_path: str) -> Dict[str, Any]:
    with open(file_path, 'r') as f:
        return json.load(f)

def is_numerically_similar(val1: Any, val2: Any, tolerance: float = 0.1) -> bool:
    """Check if two values are numerically similar within tolerance."""
    try:
        num1 = float(val1)
        num2 = float(val2)
        
        # Handle integers that should be exactly equal
        if abs(round(num1) - num1) < 1e-10 and abs(round(num2) - num2) < 1e-10:
            return round(num1) == round(num2)
        
        # For all other numbers, use relative comparison
        avg = (abs(num1) + abs(num2)) / 2
        if avg > 0:
            relative_diff = abs(num1 - num2) / avg
            return relative_diff <= (2 * tolerance)  # multiply by 2 since we're using average
        return abs(num1 - num2) <= tolerance
    except (ValueError, TypeError):
        return False

def preprocess_numbers(data: Any, tolerance: float = 1.0) -> Any:
    """Recursively round numbers to handle floating point comparisons."""
    if isinstance(data, (int, float)):
        return round(float(data))
    elif isinstance(data, dict):
        return {k: preprocess_numbers(v, tolerance) for k, v in data.items()}
    elif isinstance(data, list):
        return [preprocess_numbers(item, tolerance) for item in data]
    return data

def sort_dict_items(obj: Any) -> Any:
    """Recursively sort dictionary items to ensure consistent ordering"""
    if isinstance(obj, dict):
        return {k: sort_dict_items(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [sort_dict_items(item) for item in obj]
    return obj

def are_dicts_similar(dict1: Dict, dict2: Dict, tolerance: float) -> bool:
    """Compare two dictionaries for numerical similarity."""
    # Sort both dictionaries to ensure consistent ordering
    dict1 = sort_dict_items(dict1)
    dict2 = sort_dict_items(dict2)
    
    if dict1.keys() != dict2.keys():
        return False
    
    for key in dict1:
        val1, val2 = dict1[key], dict2[key]
        if isinstance(val1, dict) and isinstance(val2, dict):
            if not are_dicts_similar(val1, val2, tolerance):
                return False
        elif isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
            if not is_numerically_similar(val1, val2, tolerance):
                return False
        elif val1 != val2:
            return False
    return True

def format_number(value: float) -> str:
    """Format numbers according to specified rules:
    - Numbers > 1 or < -1: truncate to integer
    - Numbers between -1 and 1: 4 digits of precision
    """
    try:
        num = float(value)
        if abs(num) >= 1:
            return str(int(num))
        else:
            return f"{num:.3f}"
    except (ValueError, TypeError):
        return str(value)

def format_value(value: Any) -> Any:
    """Recursively format all numbers in a value"""
    if isinstance(value, (int, float)):
        return format_number(value)
    elif isinstance(value, dict):
        return {k: format_value(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [format_value(v) for v in value]
    return value

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

def compare_tempos(gen_tempos: List[Dict[str, Any]], ref_tempos: List[Dict[str, Any]]) -> List[str]:
    """Compare tempos at reference time points and return diff messages"""
    messages = []
    
    # Get all reference time points
    ref_time_points = [float(tempo["time"]) for tempo in ref_tempos]
    ref_time_points = sorted(list(set(ref_time_points)))  # Remove duplicates and sort
    
    # Compare tempos at each reference time point
    for time_point in ref_time_points:
        gen_bpm = tempo_at_time(gen_tempos, time_point)
        ref_bpm = tempo_at_time(ref_tempos, time_point)
        
        # If there's a significant difference, add to messages
        if abs(gen_bpm - ref_bpm) > 5:  # Allow 5 BPM difference as acceptable
            messages.append(f"At time {time_point}s: Generated={int(gen_bpm)}bpm, Reference={int(ref_bpm)}bpm")
    
    return messages

def get_diff_text(diff: DeepDiff, gen_data: Dict, ref_data: Dict, similarity_threshold: float = 0.1) -> List[str]:
    output = []
    
    # Special handling for tempos - compare at time points, not as a list
    if 'tempos' in gen_data and 'tempos' in ref_data:
        tempo_messages = compare_tempos(gen_data['tempos'], ref_data['tempos'])
        if tempo_messages:
            output.append("\n⚠️ Tempo differences:")
            for msg in tempo_messages:
                output.append(f"  {msg}")
    
    # Check values first to filter out similar numerical differences
    if 'values_changed' in diff:
        filtered_changes = {}
        for path, change in diff['values_changed'].items():
            # Skip tempos as we handled them separately
            if 'tempos' in path:
                continue
                
            old_val = sort_dict_items(change['old_value'])
            new_val = sort_dict_items(change['new_value'])
            
            # If both values are dicts, compare them
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                if not are_dicts_similar(old_val, new_val, similarity_threshold):
                    filtered_changes[path] = {
                        'old_value': format_value(old_val),
                        'new_value': format_value(new_val)
                    }
            # If both values are numbers, compare them
            elif isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                if not is_numerically_similar(old_val, new_val, similarity_threshold):
                    filtered_changes[path] = {
                        'old_value': format_number(old_val),
                        'new_value': format_number(new_val)
                    }
            # For non-numeric differences, keep them but still format any numbers
            elif old_val != new_val:
                filtered_changes[path] = {
                    'old_value': format_value(old_val),
                    'new_value': format_value(new_val)
                }
        
        if filtered_changes:
            output.append("\n⚠️ Value differences:")
            for path, change in sorted(filtered_changes.items()):
                path = path.replace("root", "")
                output.append(f"  {path}:")
                output.append(f"    Generated: {change['old_value']}")
                output.append(f"    Reference: {change['new_value']}")
    
    # Rest of the original diff text generation
    if 'dictionary_item_added' in diff:
        # Skip tempo items
        non_tempo_items = [item for item in diff['dictionary_item_added'] if 'tempos' not in item]
        if non_tempo_items:
            output.append("\n❌ Missing in generated JSON:")
            for item in sorted(non_tempo_items):
                path = item.replace("root", "")
                output.append(f"  {path}")
    
    # Check for extra items not in reference
    if 'dictionary_item_removed' in diff:
        # Skip tempo items
        non_tempo_items = [item for item in diff['dictionary_item_removed'] if 'tempos' not in item]
        if non_tempo_items:
            output.append("\n⚠️ Extra items in generated JSON:")
            for item in sorted(non_tempo_items):
                path = item.replace("root", "")
                output.append(f"  {path}")
    
    # Check for iterable differences
    if 'iterable_item_added' in diff:
        # Skip tempo items
        non_tempo_items = [item for item in diff['iterable_item_added'] if 'tempos' not in item]
        if non_tempo_items:
            output.append("\n❌ Missing array items in generated JSON:")
            for item in sorted(non_tempo_items):
                path = item.replace("root", "")
                output.append(f"  {path}")
    
    return output

def validate_jsons(generated_path: str, reference_path: str, similarity_threshold: float = 0.9) -> None:
    generated = sort_dict_items(load_json(generated_path))
    reference = sort_dict_items(load_json(reference_path))

    # Compare with updated settings and custom similarity threshold
    diff = DeepDiff(generated, reference, 
                   ignore_order=True,
                   ignore_numeric_type_changes=True,
                   ignore_type_in_groups=[(int, float)],
                   number_format_notation="f",
                   significant_digits=None)
    
    # Use our custom diff handling
    diff_lines = get_diff_text(diff, generated, reference, similarity_threshold)
    
    if not diff_lines:
        print("✅ JSONs are identical or within acceptable tolerance!")
        return
    
    # Count serious issues
    serious_issues = len([line for line in diff_lines if "❌" in line])
    if serious_issues > 0:
        print(f"\n❌ Found {serious_issues} critical difference(s)")
    
    # Print differences to console
    for line in diff_lines:
        print(line)
    
    # Write differences to file
    diff_path = os.path.splitext(generated_path)[0] + '-diff.txt'
    with open(diff_path, 'w') as f:
        f.write('\n'.join(diff_lines))
    
    print(f"\nDifferences written to: {diff_path}")

def main():
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("Usage: python json_validator.py <generated_json> <reference_json> [similarity_threshold]")
        print("similarity_threshold: Optional float between 0 and 1 (default: 0.1)")
        sys.exit(1)

    similarity_threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    if not 0 <= similarity_threshold <= 1:
        print("Error: similarity_threshold must be between 0 and 1")
        sys.exit(1)

    validate_jsons(sys.argv[1], sys.argv[2], similarity_threshold)

if __name__ == "__main__":
    main()
