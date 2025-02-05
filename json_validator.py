import json
from typing import Dict, Any, List
import sys
from deepdiff import DeepDiff
import os

def load_json(file_path: str) -> Dict[str, Any]:
    with open(file_path, 'r') as f:
        return json.load(f)

def is_numerically_similar(val1: Any, val2: Any, tolerance: float = 1.0, relative: bool = False) -> bool:
    """Check if two values are numerically similar within tolerance."""
    try:
        num1 = float(val1)
        num2 = float(val2)
        
        # Handle integers that might be represented as floats
        if abs(round(num1) - num1) < 1e-10 and abs(round(num2) - num2) < 1e-10:
            return round(num1) == round(num2)
            
        if relative:
            max_val = max(abs(num1), abs(num2))
            if max_val > 0:
                return abs(num1 - num2) / max_val < tolerance
            return abs(num1 - num2) < tolerance
            
        return abs(num1 - num2) < tolerance
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

def get_diff_text(diff: DeepDiff) -> List[str]:
    output = []
    
    if 'dictionary_item_added' in diff:
        output.append("\nMissing in generated JSON:")
        for item in diff['dictionary_item_added']:
            output.append(f"  {item}")
    
    if 'dictionary_item_removed' in diff:
        output.append("\nExtra items in generated JSON:")
        for item in diff['dictionary_item_removed']:
            output.append(f"  {item}")
    
    if 'values_changed' in diff:
        output.append("\nValue differences:")
        for path, change in diff['values_changed'].items():
            old_val, new_val = change['old_value'], change['new_value']
            
            # Handle different types of numerical comparisons
            if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                # Special handling for different value types
                if 'ticksStart' in path or 'totalTicks' in path:
                    if is_numerically_similar(old_val, new_val, tolerance=0.0001, relative=True):
                        continue
                elif 'time' in path:
                    if is_numerically_similar(old_val, new_val, tolerance=0.1):
                        continue
                elif 'ticks' in path:
                    if is_numerically_similar(old_val, new_val, tolerance=0.0001, relative=True):
                        continue
                elif 'bpm' in path:
                    if is_numerically_similar(old_val, new_val, tolerance=0.0001):
                        continue
                else:
                    if is_numerically_similar(old_val, new_val, tolerance=0.001):
                        continue
                    
            output.append(f"  {path}:")
            output.append(f"    Generated: {old_val}")
            output.append(f"    Reference: {new_val}")
    
    return output

def validate_jsons(generated_path: str, reference_path: str) -> None:
    generated = load_json(generated_path)
    reference = load_json(reference_path)

    # Compare with updated settings
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
    diff_lines = get_diff_text(diff)
    
    if not diff_lines:
        print("All numerical differences are within tolerance.")
        return
    
    # Print differences to console
    for line in diff_lines:
        print(line)
    
    # Write differences to file
    diff_path = os.path.splitext(generated_path)[0] + '-diff.txt'
    with open(diff_path, 'w') as f:
        f.write('\n'.join(diff_lines))
    
    print(f"\nDifferences written to: {diff_path}")

def main():
    if len(sys.argv) != 3:
        print("Usage: python json_validator.py <generated_json> <reference_json>")
        sys.exit(1)

    validate_jsons(sys.argv[1], sys.argv[2])

if __name__ == "__main__":
    main()
