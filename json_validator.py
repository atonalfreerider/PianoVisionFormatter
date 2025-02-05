import json
from typing import Dict, Any
import sys
from deepdiff import DeepDiff

def load_json(file_path: str) -> Dict[str, Any]:
    with open(file_path, 'r') as f:
        return json.load(f)

def validate_jsons(generated_path: str, reference_path: str) -> None:
    generated = load_json(generated_path)
    reference = load_json(reference_path)

    # Use DeepDiff to compare the JSONs
    diff = DeepDiff(generated, reference, ignore_order=True)
    
    if not diff:
        print("✅ JSONs are identical!")
        return

    print("❌ Differences found:")
    if 'dictionary_item_added' in diff:
        print("\nMissing in generated JSON:")
        for item in diff['dictionary_item_added']:
            print(f"  {item}")
    
    if 'dictionary_item_removed' in diff:
        print("\nExtra items in generated JSON:")
        for item in diff['dictionary_item_removed']:
            print(f"  {item}")
    
    if 'values_changed' in diff:
        print("\nValue differences:")
        for path, change in diff['values_changed'].items():
            print(f"  {path}:")
            print(f"    Generated: {change['old_value']}")
            print(f"    Reference: {change['new_value']}")

def main():
    if len(sys.argv) != 3:
        print("Usage: python json_validator.py <generated_json> <reference_json>")
        sys.exit(1)

    validate_jsons(sys.argv[1], sys.argv[2])

if __name__ == "__main__":
    main()
