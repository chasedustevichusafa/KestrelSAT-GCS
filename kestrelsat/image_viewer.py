"""On-demand JPEG decoding and Tk viewing; never starts a camera stream."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from . import scaling


def decode_image(path):
    from PIL import Image
    with Image.open(path) as image:
        if image.format != 'JPEG' or image.width*image.height > 8_000_000 or min(image.size) < 1:
            raise ValueError('Expected a JPEG no larger than 8 megapixels')
        image.load()
        return image.convert('RGB')


class ImageViewer:
    def __init__(self, app, path, image, on_close):
        self.app, self.path, self.original, self.on_close = app, Path(path), image, on_close
        self.closed = False
        self.timer = None
        self.photo = None
        self.zoom = 1.0
        self.fit = True
        self.window = tk.Toplevel(app.root)
        self.window.title('Saved image | '+self.path.name)
        width, height = scaling.clamp_size((scaling.px(app.ui_scale, 900), scaling.px(app.ui_scale, 650)),
                                           scaling.work_area(app.root))
        self.window.geometry(f'{width}x{height}')
        controls = ttk.Frame(self.window, padding=8)
        controls.pack(fill=tk.X)
        for label, command in [('Fit', self.fit_image), ('-', lambda: self.change_zoom(.8)),
                               ('+', lambda: self.change_zoom(1.25)), ('100%', self.full_size)]:
            ttk.Button(controls, text=label, command=command).pack(side=tk.LEFT, padx=(0, 6))
        self.info = tk.StringVar()
        ttk.Label(controls, textvariable=self.info).pack(side=tk.LEFT)
        self.path_label = ttk.Label(self.window, text='Local copy: '+str(self.path), wraplength=800)
        self.path_label.pack(anchor=tk.W, fill=tk.X, padx=8)
        self.window.bind('<Configure>', self.resize_label)
        pane = ttk.Frame(self.window)
        pane.pack(fill=tk.BOTH, expand=True)
        pane.rowconfigure(0, weight=1); pane.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(pane, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        xbar = ttk.Scrollbar(pane, orient=tk.HORIZONTAL, command=self.canvas.xview)
        xbar.grid(row=1, column=0, sticky='ew')
        ybar = ttk.Scrollbar(pane, orient=tk.VERTICAL, command=self.canvas.yview)
        ybar.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.canvas.bind('<Configure>', self.schedule)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda event: self.close())
        app.themes.restyle(self.window)
        self.schedule()

    def resize_label(self, event):
        if event.widget is self.window and not self.closed:
            self.path_label.configure(wraplength=max(160, event.width-16))

    def schedule(self, event=None):
        if self.closed:
            return
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.timer = self.window.after(80, self.render)

    def render(self):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        if self.closed:
            return
        from PIL import Image, ImageTk
        w, h = self.original.size
        if self.fit:
            self.zoom = min(1, max(.01, (self.canvas.winfo_width()-8)/w), max(.01, (self.canvas.winfo_height()-8)/h))
        size = (max(1, int(w*self.zoom)), max(1, int(h*self.zoom)))
        resized = self.original.resize(size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(resized, master=self.window)
        resized.close()
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, *size))
        self.info.set(f'{w} × {h} | {self.zoom*100:.0f}%')

    def fit_image(self):
        self.fit = True; self.schedule()

    def change_zoom(self, factor):
        self.fit = False; self.zoom = max(.05, min(1, self.zoom*factor)); self.schedule()

    def full_size(self):
        self.fit = False; self.zoom = 1; self.schedule()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.photo = None
        self.info = None  # Release Tk variables on the Tk thread, before destroying the window.
        self.original.close()
        self.window.destroy()
        self.on_close(self)
