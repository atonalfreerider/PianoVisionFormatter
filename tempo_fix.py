import json
import sys
import os
from typing import Dict, List, Any, Tuple

def load_json(file_path: str) -> Dict[str, Any]:
    """Load JSON file and return as dictionary"""
    with open(file_path, 'r') as f:
        return json.load(f)

def save_json(data: Dict[str, Any], file_path: str) -> None:
    """Save dictionary as JSON file"""
    with open(file_path, 'w') as f:
        json.dump(data, f)

def map_tempos(reference_file: str, generated_file: str) -> Dict[str, Any]:
    """Map tempo values from reference file to generated file at same time points"""
    # Load files
    reference = load_json(reference_file)
    generated = load_json(generated_file)
    
    # Extract reference tempos and sort by time
    ref_tempos = sorted(reference.get("tempos", []), key=lambda x: float(x["time"]))
    
    # Create a copy of the generated data that we'll modify
    modified = generated.copy()
    
    # Replace the tempos in the generated file with the reference tempos
    modified["tempos"] = ref_tempos
    
    # Return the modified data
    return modified

def main():
    if len(sys.argv) != 3:
        print("Usage: python tempo_fix.py <generated_json> <reference_json>")
        sys.exit(1)
    
    generated_file = sys.argv[1]
    reference_file = sys.argv[2]
    
    # Check if files exist
    if not os.path.exists(generated_file):
        print(f"Error: {generated_file} does not exist")
        sys.exit(1)
    if not os.path.exists(reference_file):
        print(f"Error: {reference_file} does not exist")
        sys.exit(1)
    
    # Map tempos from reference to generated
    modified_data = map_tempos(reference_file, generated_file)
    
    # Create output filename
    output_file = os.path.splitext(generated_file)[0] + '_fixed.json'
    
    # Save modified data
    save_json(modified_data, output_file)
    
    print(f"Tempo-fixed file saved as: {output_file}")

if __name__ == "__main__":
    main()
