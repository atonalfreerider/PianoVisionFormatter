import json
import tkinter as tk
from tkinter import ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import numpy as np

class PianoRollComparator(tk.Tk):
    def __init__(self):
        super().__init__()

        self._resize_job = self.after(100, self.update_plot)
        self.title("Piano Roll Comparison")
        self.geometry("1200x800")
        
        # Create main frame
        main_frame = ttk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Button frame at top
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Button(btn_frame, text="Load Generated File", command=self.load_generated).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Load Reference File", command=self.load_reference).pack(side=tk.LEFT, padx=5)

        # Add zoom controls
        zoom_frame = ttk.Frame(btn_frame)
        zoom_frame.pack(side=tk.LEFT, padx=20)
        ttk.Button(zoom_frame, text="Zoom In", command=self.zoom_in).pack(side=tk.LEFT, padx=2)
        ttk.Button(zoom_frame, text="Zoom Out", command=self.zoom_out).pack(side=tk.LEFT, padx=2)

        # Create scrolled frame setup
        self.scroll_frame = ttk.Frame(main_frame)
        self.scroll_frame.pack(fill=tk.BOTH, expand=True)
        
        # Add scrollbar
        self.scrollbar = ttk.Scrollbar(self.scroll_frame, orient=tk.VERTICAL)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Create figure first
        self.fig = Figure(figsize=(12, 40), dpi=100)
        
        # Create matplotlib canvas
        self.canvas_widget = FigureCanvasTkAgg(self.fig, master=self.scroll_frame)
        self.plot_widget = self.canvas_widget.get_tk_widget()
        self.plot_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Configure scrolling
        self.plot_widget.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.configure(command=self.plot_widget.yview)
        
        # Initialize state
        self.generated_data = None
        self.reference_data = None
        self.zoom_level = 1.0
        
        # Bind events
        self.bind("<Configure>", self.on_window_resize)
        self.plot_widget.bind('<Configure>', self._on_plot_configure)

    def load_generated(self):
        file_path = tk.filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if file_path:
            with open(file_path, 'r') as f:
                self.generated_data = json.load(f)
            self.update_plot()

    def load_reference(self):
        file_path = tk.filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if file_path:
            with open(file_path, 'r') as f:
                self.reference_data = json.load(f)
            self.update_plot()

    @staticmethod
    def create_piano_roll_data(tracks_v2, hand="right"):
        """Convert track data to piano roll format"""
        notes = []
        times = []
        durations = []
        colors = []
        
        track_data = tracks_v2[hand]
        for measure in track_data:
            for note in measure['notes']:
                # Rescale MIDI notes to 0-88 range
                notes.append(note['note'] - 21)  # Subtract lowest MIDI note (21)
                times.append(note['start'])
                durations.append(note['duration'])
                colors.append('blue' if hand == "right" else 'red')
        
        return np.array(notes), np.array(times), np.array(durations), colors

    def zoom_in(self):
        self.zoom_level *= 0.8
        self.update_plot()

    def zoom_out(self):
        self.zoom_level *= 1.25
        self.update_plot()

    def on_window_resize(self, event):
        if event.widget != self:
            return
        if hasattr(self, '_resize_job'):
            self.after_cancel(self._resize_job)

    def _on_plot_configure(self, event):
        """Handle plot widget configuration"""
        if hasattr(self, 'after_id'):
            self.after_cancel(self.after_id)
        self.after_id = self.after(100, self._update_scroll_region)

    def update_plot(self):
        """Update the visualization with current data"""
        if not self.generated_data or not self.reference_data:
            return

        # Calculate proper width based on window size
        window_width = self.winfo_width()
        plot_width = max(window_width / self.fig.dpi - 1, 8)  # Minimum width of 8 inches

        # Update figure width only, maintain fixed height
        self.fig.set_size_inches(plot_width, 40, forward=True)
        
        # Clear and create new subplot
        self.fig.clear()
        ax = self.fig.add_subplot(111)

        # Plot data
        self._plot_data(ax)
        
        # Update layout with wider margins
        self.fig.subplots_adjust(left=0.15, right=0.85, top=0.98, bottom=0.05)
        
        # Draw the plot
        self.canvas_widget.draw()
        
        # Update scroll region
        self._update_scroll_region()

    def _update_scroll_region(self):
        """Update the scroll region to match the plot size"""
        bbox = self.plot_widget.bbox("all")
        if bbox:
            height = self.fig.get_figheight() * self.fig.dpi
            self.plot_widget.configure(scrollregion=(0, 0, bbox[2], height))

    def _plot_data(self, ax):
        """Plot all data to the given axes"""
        REF_OFFSET = -0.3
        GEN_OFFSET = 0.3
        max_time = 0

        # Plot hands and find max time
        for hand in ['right', 'left']:
            gen_notes, gen_times, _, _ = self.create_piano_roll_data(
                self.generated_data['tracksV2'], hand)
            ref_notes, ref_times, _, _ = self.create_piano_roll_data(
                self.reference_data['tracksV2'], hand)
            
            # Update max_time
            if len(gen_times) > 0:
                max_time = max(max_time, gen_times.max())
            if len(ref_times) > 0:
                max_time = max(max_time, ref_times.max())
            
            color_gen = 'blue' if hand == 'right' else 'red'
            color_ref = 'green' if hand == 'right' else 'orange'
            
            ax.scatter(ref_notes + REF_OFFSET, ref_times, c=color_ref, alpha=0.5,
                      label=f'Reference {hand.title()}', marker='|')
            ax.scatter(gen_notes + GEN_OFFSET, gen_times, c=color_gen, alpha=0.5,
                      label=f'Generated {hand.title()}', marker='|')

        # Set limits and labels
        center = 44
        half_width = 45 * self.zoom_level
        ax.set_xlim(center - half_width, center + half_width)
        ax.set_ylim(-max_time * 0.02, max_time * 1.1)  # Add padding at top and bottom
        
        # Style the plot
        ax.set_title('Piano Roll Comparison', fontsize=12, pad=20)
        ax.set_xlabel('Piano Key (1-88)', fontsize=10, labelpad=10)
        ax.set_ylabel('Time (seconds)', fontsize=10, labelpad=10)
        ax.grid(True, alpha=0.3)
        ax.tick_params(axis='both', labelsize=9)
        ax.legend(fontsize=9)

        # Add measure lines and tempo changes
        self._add_measures_and_tempos(ax, max_time)

    def _add_measures_and_tempos(self, ax, max_time):
        """Add measure lines and tempo markings for both reference and generated files"""
        x_min, x_max = ax.get_xlim()
        
        # Constants for positioning - increased spacing
        ref_measure_offset = x_min - 8  # Left side position for reference measures
        gen_measure_offset = x_min - 4  # Left side position for generated measures
        
        ref_tempo_offset = x_max + 3    # Right side position for reference tempos
        gen_tempo_offset = x_max + 8    # Right side position for generated tempos
        
        # Reference file - measures (purple)
        for i, measure in enumerate(self.reference_data['measures']):
            time = measure['time']
            # Horizontal line across the piano roll
            ax.plot([x_min, x_max], [time, time], 'purple', linestyle='--', alpha=0.2)
            # Reference measure marker
            ax.text(ref_measure_offset, time, f"R{i+1}", fontsize=8, 
                   verticalalignment='bottom', color='purple', alpha=0.8)

        # Generated file - measures (blue)
        for i, measure in enumerate(self.generated_data['measures']):
            time = measure['time']
            # Horizontal line across the piano roll
            ax.plot([x_min, x_max], [time, time], 'blue', linestyle='--', alpha=0.2)
            # Generated measure marker
            ax.text(gen_measure_offset, time, f"G{i+1}", fontsize=8, 
                   verticalalignment='bottom', color='blue', alpha=0.8)

        # Reference file - tempo changes (purple)
        for tempo in self.reference_data['tempos']:
            time = tempo['time']
            ax.plot([x_max - 1, x_max], [time, time], 'purple', linewidth=2, alpha=0.8)
            ax.text(ref_tempo_offset, time, f"R: {int(tempo['bpm'])} BPM", 
                   fontsize=8, color='purple', alpha=0.8, horizontalalignment='left')

        # Generated file - tempo changes (blue)
        for tempo in self.generated_data['tempos']:
            time = tempo['time']
            ax.plot([x_max - 1, x_max], [time, time], 'blue', linewidth=2, alpha=0.8)
            ax.text(gen_tempo_offset, time, f"G: {int(tempo['bpm'])} BPM", 
                   fontsize=8, color='blue', alpha=0.8, horizontalalignment='left')
        
        # Adjust the subplot to make room for the markers - increased margins
        self.fig.subplots_adjust(left=0.18, right=0.82, top=0.98, bottom=0.05)

def main():
    import sys
    
    if len(sys.argv) == 3:
        # If two arguments provided, load files directly and show comparison
        app = PianoRollComparator()
        try:
            with open(sys.argv[1], 'r') as f:
                app.generated_data = json.load(f)
            with open(sys.argv[2], 'r') as f:
                app.reference_data = json.load(f)
            app.update_plot()
            app.mainloop()
        except Exception as e:
            print(f"Error loading files: {e}")
            sys.exit(1)
    elif len(sys.argv) == 1:
        # No arguments, run in interactive mode
        app = PianoRollComparator()
        app.mainloop()
    else:
        print("Usage: python compare_piano_rolls.py [generated.json reference.json]")
        sys.exit(1)

if __name__ == "__main__":
    main()
