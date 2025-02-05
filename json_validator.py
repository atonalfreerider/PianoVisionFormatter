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

def are_dicts_similar(dict1: Dict, dict2: Dict, tolerance: float) -> bool:
    """Compare two dictionaries for numerical similarity."""
    if dict1.keys() != dict2.keys():
        return False
    
    for key in dict1:
        val1, val2 = dict1[key], dict2[key]
        if isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
            if not is_numerically_similar(val1, val2, tolerance):
                return False
        elif val1 != val2:
            return False
    return True

def get_diff_text(diff: DeepDiff, similarity_threshold: float = 0.1) -> List[str]:
    output = []
    
    # Check for missing items in generated JSON
    if 'dictionary_item_added' in diff:
        output.append("\n❌ Missing in generated JSON:")
        for item in sorted(diff['dictionary_item_added']):
            path = item.replace("root", "")
            output.append(f"  {path}")
    
    # Check for extra items not in reference
    if 'dictionary_item_removed' in diff:
        output.append("\n⚠️ Extra items in generated JSON:")
        for item in sorted(diff['dictionary_item_removed']):
            path = item.replace("root", "")
            output.append(f"  {path}")
    
    # Check for value differences
    if 'values_changed' in diff:
        output.append("\n⚠️ Value differences:")
        for path, change in sorted(diff['values_changed'].items()):
            old_val, new_val = change['old_value'], change['new_value']
            
            # Skip if values are numerically similar within threshold
            if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                if is_numerically_similar(old_val, new_val, similarity_threshold):
                    continue
            
            path = path.replace("root", "")
            output.append(f"  {path}:")
            output.append(f"    Generated: {old_val}")
            output.append(f"    Reference: {new_val}")
    
    # Check for iterable differences
    if 'iterable_item_added' in diff:
        output.append("\n❌ Missing array items in generated JSON:")
        for item in sorted(diff['iterable_item_added']):
            path = item.replace("root", "")
            output.append(f"  {path}")
    
    return output

def validate_jsons(generated_path: str, reference_path: str, similarity_threshold: float = 0.1) -> None:
    generated = load_json(generated_path)
    reference = load_json(reference_path)

    # Compare with updated settings and custom similarity threshold
    diff = DeepDiff(generated, reference, 
                    ignore_order=True,
                    ignore_numeric_type_changes=True,
                    ignore_type_in_groups=[(int, float)],
                    number_format_notation="f",
                    significant_digits=None)
    
    if not diff:
        print("✅ JSONs are identical!")
        return

    print("❌ Differences found:")
    # Pass similarity threshold to get_diff_text
    diff_lines = get_diff_text(diff, similarity_threshold)
    
    if not diff_lines:
        print("✅ All differences are within acceptable tolerance.")
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
