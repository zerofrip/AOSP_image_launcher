import tkinter as tk
from tkinter import filedialog, messagebox

class ROMCreatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Custom ROM Creator (legacy mock)")
        self.root.geometry("400x300")

        self.base_image = tk.StringVar()
        self.output_name = tk.StringVar(value="custom_rom.img")

        self.create_widgets()

    def create_widgets(self):
        frame = tk.Frame(self.root, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text="Legacy mock — this tool does not create a ROM.",
            fg="#a60",
        ).pack(anchor="w", pady=(0, 10))
        tk.Label(frame, text="Base System Image:").pack(anchor="w")
        entry_base = tk.Entry(frame, textvariable=self.base_image, width=40)
        entry_base.pack(fill="x", pady=(0, 10))
        tk.Button(frame, text="Browse...", command=self.browse_base).pack(anchor="e")

        tk.Label(frame, text="Output ROM Name:").pack(anchor="w", pady=(10, 0))
        tk.Entry(frame, textvariable=self.output_name, width=40).pack(fill="x", pady=(0, 20))

        tk.Button(frame, text="Create Custom ROM (legacy mock)", command=self.create_rom).pack(fill="x")

    def browse_base(self):
        path = filedialog.askopenfilename(title="Select Base Image", filetypes=[("Image files", "*.img"), ("All files", "*.*")])
        if path:
            self.base_image.set(path)

    def create_rom(self):
        if not self.base_image.get():
            messagebox.showwarning("Warning", "Please select a base system image.")
            return
        messagebox.showinfo(
            "Legacy mock",
            "Custom ROM Creator is a non-functional legacy mock. "
            f"No ROM was created ({self.output_name.get()} was not written).",
        )

def launch():
    root = tk.Toplevel()
    app = ROMCreatorGUI(root)
    return app

if __name__ == "__main__":
    root = tk.Tk()
    app = ROMCreatorGUI(root)
    root.mainloop()
