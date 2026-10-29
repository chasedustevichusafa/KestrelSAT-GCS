"""Single live-preview pop-out: resizable viewport, fixed native image pixels.

This view borrows frames only while constructing a Tk PhotoImage. It never owns
or closes the source Pillow image or starts its own camera/serial worker. Resize
events change item coordinates/scroll bounds; they never resample the bitmap.
"""
import tkinter as tk
from tkinter import ttk

from . import scaling


class PreviewWindow:
    def __init__(self, tab):
        self.tab = tab
        self.closed = False
        self.photo = None
        self.dimensions = None
        self.window = tk.Toplevel(tab.app.root)
        self.window.title('Live Preview | Native image size')
        area = scaling.work_area(tab.app.root)
        image = tab.image
        size = (max(680, image.width+40), max(500, image.height+180)) if image is not None else (800, 600)
        width, height = scaling.clamp_size(size, area)
        self.window.geometry(f'{width}x{height}')
        self.window.minsize(*scaling.clamp_size((360, 260), area))
        self.window.resizable(True, True)
        toolbar = ttk.Frame(self.window, padding=8)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text='Stop preview', command=tab.stop).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text='Save frame...', command=tab.save_frame).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text='Return to tab', command=self.return_to_tab).pack(side=tk.LEFT)
        self.native_size = tk.StringVar(value='Waiting for the first frame | native size, no stretching')
        self.native_label = ttk.Label(self.window, textvariable=self.native_size, padding=(8, 0))
        self.native_label.pack(fill=tk.X)
        self.status = ttk.Label(self.window, textvariable=tab.status, padding=(8, 4), wraplength=700)
        self.status.pack(fill=tk.X)
        self.details = ttk.Label(self.window, textvariable=tab.details, padding=(8, 0), wraplength=700)
        self.details.pack(fill=tk.X)
        self.advice = ttk.Label(self.window, textvariable=tab.advice, padding=(8, 4), wraplength=700)
        self.advice.pack(fill=tk.X)
        pane = ttk.Frame(self.window, padding=8)
        pane.pack(fill=tk.BOTH, expand=True)
        pane.rowconfigure(0, weight=1); pane.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(pane, highlightthickness=0, background=tab.app.themes.colors['field_bg'])
        self.canvas.grid(row=0, column=0, sticky='nsew')
        horizontal = ttk.Scrollbar(pane, orient=tk.HORIZONTAL, command=self.canvas.xview)
        vertical = ttk.Scrollbar(pane, orient=tk.VERTICAL, command=self.canvas.yview)
        horizontal.grid(row=1, column=0, sticky='ew')
        vertical.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.item = self.canvas.create_text(0, 0, text='Waiting for a preview frame',
                                            fill=tab.app.themes.colors['fg'])
        self.canvas.bind('<Configure>', self.layout)
        self.window.bind('<Configure>', self.wrap)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda event: self.close())
        tab.app.themes.restyle(self.window)
        if image is not None:
            try:
                self.show_image(image)
            except Exception:
                self.close()
                raise

    def show_image(self, image):
        if self.closed:
            return
        from PIL import ImageTk
        # Pass the original pixels directly. Enlarging/shrinking this window must
        # never change PhotoImage.width/height or the decoded frame on the tab.
        photo = ImageTk.PhotoImage(image, master=self.canvas)
        self.canvas.delete('all')
        self.item = self.canvas.create_image(0, 0, anchor=tk.CENTER, image=photo)
        self.photo = photo
        self.dimensions = image.size
        self.native_size.set(f'{image.width}×{image.height} | 100% native pixels | resize window or scroll to view')
        self.layout()

    def layout(self, event=None):
        if self.closed:
            return
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        if self.dimensions is not None:
            width = max(width, self.dimensions[0])
            height = max(height, self.dimensions[1])
        self.canvas.configure(scrollregion=(0, 0, width, height))
        self.canvas.coords(self.item, width/2, height/2)

    def wrap(self, event):
        if event.widget is self.window and not self.closed:
            width = max(160, event.width-24)
            for label in (self.status, self.details, self.advice):
                label.configure(wraplength=width)

    def return_to_tab(self):
        self.tab.app.notebook.select(self.tab.frame)
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.photo = None
        self.window.destroy()
        self.native_size = None # Release the Tk variable on Tk's thread.
        if self.tab.popout is self:
            self.tab.popout = None
