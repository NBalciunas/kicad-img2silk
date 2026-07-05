import os
import random

try:
    import pcbnew
    import wx
except ImportError:
    pcbnew = wx = None

MAX_PX = 1000
PX_PER_MM = 10.0
DOT_MAX_PX = 400

_KERNELS = {
    1: (16, ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1))),
    2: (8, ((1, 0, 1), (2, 0, 1), (-1, 1, 1),
            (0, 1, 1), (1, 1, 1), (0, 2, 1))),
}

_BAYER = (
    (0, 32, 8, 40, 2, 34, 10, 42),
    (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38),
    (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41),
    (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 7, 39, 13, 45, 5, 37),
    (63, 31, 55, 23, 61, 29, 53, 21),
)


_ALGOS = (("Atkinson", 2), ("Bayer 8×8", 3), ("Floyd–Steinberg", 1),
          ("Random", 4), ("Threshold", 0))


def _dither(grey, w, h, alpha, thresh, inv, algo):
    """grey = RGB bytes of a greyscale image; returns bytearray of 0/1 (1 = dark).
    algo: 0 threshold, 1 Floyd-Steinberg, 2 Atkinson, 3 Bayer 8x8, 4 random."""
    n = w * h
    if algo in _KERNELS:
        div, kernel = _KERNELS[algo]
        px = [float(grey[i * 3]) for i in range(n)]
        if alpha is not None:
            blank = 0.0 if inv else 255.0
            for i in range(n):
                if alpha[i] < 128:
                    px[i] = blank
        on = bytearray(n)
        i = 0
        for y in range(h):
            for x in range(w):
                old = px[i]
                dark = old < thresh
                err = (old if dark else old - 255.0) / div
                on[i] = dark != inv
                for dx, dy, wt in kernel:
                    xx = x + dx
                    if 0 <= xx < w and y + dy < h:
                        px[i + dy * w + dx] += err * wt
                i += 1
    elif algo == 3:
        bias = thresh - 128
        on = bytearray(n)
        i = 0
        for y in range(h):
            brow = _BAYER[y & 7]
            for x in range(w):
                on[i] = (grey[i * 3] < brow[x & 7] * 4 + 2 + bias) != inv
                i += 1
    elif algo == 4:
        rnd = random.Random(0)
        bias = 128 - thresh
        on = bytearray((grey[i * 3] + bias < rnd.randrange(256)) != inv
                       for i in range(n))
    else:
        lut = bytes(((v < thresh) != inv) for v in range(256))
        on = bytearray(grey[::3].translate(lut))
    if alpha is not None:
        for i in range(n):
            if alpha[i] < 128:
                on[i] = 0
    return on


def _runs(bits):
    out, start = [], None
    for i, b in enumerate(bits):
        if b and start is None:
            start = i
        elif not b and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(bits)))
    return out


def _rects(on, w, h):
    """Merge identical runs in consecutive rows into rectangles (xa, xb, ya, yb)."""
    out, active = [], {}
    for y in range(h + 1):
        runs = set(_runs(on[y * w:(y + 1) * w])) if y < h else set()
        for r in [k for k in active if k not in runs]:
            out.append((r[0], r[1], active.pop(r), y))
        for r in runs:
            active.setdefault(r, y)
    return out


if wx:

    class Img2SilkDialog(wx.Dialog):
        def __init__(self, parent, board):
            super().__init__(parent, title="Img2Silk")
            self.board = board
            self.image = None

            s = wx.BoxSizer(wx.VERTICAL)
            title = wx.StaticText(self, label="Img2Silk v1.1")
            title.SetFont(title.GetFont().Bold().Scaled(1.4))
            s.Add(title, 0, wx.ALL | wx.ALIGN_CENTER, 10)

            btn_import = wx.Button(self, label="Import Image")
            btn_import.Bind(wx.EVT_BUTTON, self.on_import)
            s.Add(btn_import, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

            previews = wx.BoxSizer(wx.HORIZONTAL)
            self.preview_orig = wx.StaticBitmap(self, size=(280, 280))
            self.preview_bw = wx.StaticBitmap(self, size=(280, 280))
            previews.Add(self.preview_orig, 0, wx.RIGHT, 10)
            previews.Add(self.preview_bw, 0)
            s.Add(previews, 0, wx.ALL | wx.ALIGN_CENTER, 10)

            grid = wx.FlexGridSizer(8, 2, 5, 5)
            grid.AddGrowableCol(1)
            grid.Add(wx.StaticText(self, label="Dithering algorithm:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.dither = wx.Choice(self, choices=[n for n, _ in _ALGOS])
            self.dither.SetSelection(4)
            self.dither.Disable()
            self.dither.Bind(wx.EVT_CHOICE, self.update_bw)
            grid.Add(self.dither, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Black / white threshold:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.threshold = wx.Slider(self, value=50, minValue=0, maxValue=100,
                                       style=wx.SL_HORIZONTAL | wx.SL_LABELS)
            self.threshold.Disable()
            self.threshold.Bind(wx.EVT_SLIDER, self.update_bw)
            grid.Add(self.threshold, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Pixel Scale:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.dot_size = wx.Slider(self, value=25, minValue=15, maxValue=150)
            self.dot_size.Disable()
            self.dot_size.Bind(wx.EVT_SLIDER, self.on_dot_size)
            self.dot_val = wx.StaticText(self, label="0.25")
            dot_col = wx.BoxSizer(wx.VERTICAL)
            dot_col.Add(self.dot_size, 0, wx.EXPAND)
            dot_col.Add(self.dot_val, 0, wx.ALIGN_CENTER_HORIZONTAL)
            dot_row = wx.BoxSizer(wx.HORIZONTAL)
            dot_row.Add(wx.StaticText(self, label="0.15"), 0, wx.ALIGN_CENTER_VERTICAL)
            dot_row.Add(dot_col, 1, wx.EXPAND)
            dot_row.Add(wx.StaticText(self, label="1.50"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(dot_row, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Layer:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.layer = wx.Choice(self, choices=["Copper", "Silkscreen", "Solder Mask"])
            self.layer.SetSelection(1)
            self.layer.Disable()
            grid.Add(self.layer, 1, wx.EXPAND)
            grid.Add((0, 0))
            self.invert = wx.CheckBox(self, label="Invert colors")
            self.invert.Disable()
            self.invert.Bind(wx.EVT_CHECKBOX, self.update_bw)
            grid.Add(self.invert, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Length (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.length = wx.TextCtrl(self)
            self.length.Disable()
            self.length.Bind(wx.EVT_TEXT, self.on_length)
            grid.Add(self.length, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Width (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.width = wx.TextCtrl(self)
            self.width.Disable()
            self.width.Bind(wx.EVT_TEXT, self.on_width)
            grid.Add(self.width, 1, wx.EXPAND)
            grid.Add((0, 0))
            self.keep_aspect = wx.CheckBox(self, label="Maintain aspect ratio")
            self.keep_aspect.SetValue(True)
            self.keep_aspect.Disable()
            self.keep_aspect.Bind(wx.EVT_CHECKBOX, self.on_keep_aspect)
            grid.Add(self.keep_aspect, 1, wx.EXPAND)
            s.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

            btn_create = wx.Button(self, label="Insert Graphics")
            btn_create.Bind(wx.EVT_BUTTON, self.on_create)
            s.Add(btn_create, 0, wx.ALL | wx.EXPAND, 10)

            self.SetSizerAndFit(s)
            self.CentreOnParent()

        def on_import(self, _):
            with wx.FileDialog(self, "Choose an image",
                               wildcard="Images (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg",
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fd:
                if fd.ShowModal() != wx.ID_OK:
                    return
                path = fd.GetPath()
            img = wx.Image(path)
            if not img.IsOk():
                wx.MessageBox("Could not load image.", "Img2Silk", wx.ICON_ERROR)
                return
            self.image = img
            w, h = img.GetWidth(), img.GetHeight()
            k = min(280.0 / w, 280.0 / h, 1.0)
            prev = img.Scale(max(1, int(w * k)), max(1, int(h * k)), wx.IMAGE_QUALITY_HIGH)
            self.preview_orig.SetBitmap(wx.Bitmap(prev))
            grey = prev.ConvertToGreyscale()
            self._pw, self._ph = grey.GetWidth(), grey.GetHeight()
            self._grey = bytes(grey.GetData())
            self._alpha = bytes(grey.GetAlphaBuffer()) if grey.HasAlpha() else None
            self._aspect = float(h) / w
            self.dither.Enable()
            self.threshold.Enable()
            self.invert.Enable()
            self.length.Enable()
            self.width.Enable()
            self.keep_aspect.Enable()
            self.layer.Enable()
            self.length.ChangeValue("50")
            self.width.ChangeValue("%.2f" % (50.0 * self._aspect))
            self.update_bw()
            self.Fit()
            self.Layout()

        def _mm(self, ctrl):
            try:
                v = float(ctrl.GetValue())
                return v if v > 0 else None
            except ValueError:
                return None

        def on_length(self, _):
            if self.image is None:
                return
            if self.keep_aspect.GetValue():
                v = self._mm(self.length)
                if v:
                    self.width.ChangeValue("%.2f" % (v * self._aspect))
            self.update_bw()

        def on_width(self, _):
            if self.image is None:
                return
            if self.keep_aspect.GetValue():
                v = self._mm(self.width)
                if v:
                    self.length.ChangeValue("%.2f" % (v / self._aspect))
            self.update_bw()

        def on_keep_aspect(self, _):
            if self.keep_aspect.GetValue():
                self.on_length(None)

        def on_dot_size(self, _):
            self.dot_val.SetLabel("%.2f" % (self.dot_size.GetValue() / 100.0))
            self.update_bw()

        def _out_grid(self, dotted, length, width):
            """The pixel grid on_create will draw — preview uses it too (WYSIWYG)."""
            ppmm = 100.0 / self.dot_size.GetValue() if dotted else PX_PER_MM
            max_px = DOT_MAX_PX if dotted else MAX_PX
            w_px = int(min(self.image.GetWidth(), max(16, length * ppmm), max_px))
            h_px = int(min(self.image.GetHeight(), max(16, width * ppmm), max_px))
            return w_px, h_px

        def update_bw(self, _=None):
            if self.image is None:
                return
            algo = _ALGOS[self.dither.GetSelection()][1]
            self.dot_size.Enable(algo != 0)
            length, width = self._mm(self.length), self._mm(self.width)
            if length and width:
                w_px, h_px = self._out_grid(algo != 0, length, width)
                img = self.image.ConvertToGreyscale()
                img.Rescale(w_px, h_px, wx.IMAGE_QUALITY_HIGH)
                grey = bytes(img.GetData())
                alpha = bytes(img.GetAlphaBuffer()) if img.HasAlpha() else None
            else:
                grey, alpha, w_px, h_px = self._grey, self._alpha, self._pw, self._ph
            on = _dither(grey, w_px, h_px, alpha,
                         self.threshold.GetValue() * 255 // 100,
                         self.invert.GetValue(), algo)
            v = bytes(on).translate(bytes([255] + [0] * 255))
            out = bytearray(len(v) * 3)
            out[0::3] = out[1::3] = out[2::3] = v
            bw = wx.Image(w_px, h_px, bytes(out))
            k = min(280.0 / w_px, 280.0 / h_px)
            bw.Rescale(max(1, int(w_px * k)), max(1, int(h_px * k)),
                       wx.IMAGE_QUALITY_HIGH if k < 1 else wx.IMAGE_QUALITY_NORMAL)
            self.preview_bw.SetBitmap(wx.Bitmap(bw))

        def on_create(self, _):
            if self.image is None:
                wx.MessageBox("Import an image first.", "Img2Silk", wx.ICON_WARNING)
                return
            try:
                length = float(self.length.GetValue())
                width = float(self.width.GetValue())
                if length <= 0 or width <= 0:
                    raise ValueError
            except ValueError:
                wx.MessageBox("Length and Width must be positive numbers.",
                              "Img2Silk", wx.ICON_WARNING)
                return

            algo = _ALGOS[self.dither.GetSelection()][1]
            busy = wx.BusyCursor()
            w_px, h_px = self._out_grid(algo != 0, length, width)
            img = self.image.ConvertToGreyscale()
            img.Rescale(w_px, h_px, wx.IMAGE_QUALITY_HIGH)
            on = _dither(bytes(img.GetData()), w_px, h_px,
                         bytes(img.GetAlphaBuffer()) if img.HasAlpha() else None,
                         self.threshold.GetValue() * 255 // 100,
                         not self.invert.GetValue(), algo)

            bbox = self.board.GetBoardEdgesBoundingBox()
            if bbox.GetWidth() > 0 and bbox.GetHeight() > 0:
                cx = pcbnew.ToMM(bbox.GetCenter().x)
                cy = pcbnew.ToMM(bbox.GetCenter().y)
            else:
                cx, cy = 100.0, 100.0
            x0, y0 = cx - length / 2.0, cy - width / 2.0

            rects = _rects(on, w_px, h_px)
            if not rects:
                wx.MessageBox("No pixels selected at this threshold. Try moving the slider.",
                              "Img2Silk", wx.ICON_WARNING)
                return
            if len(rects) > 30000 and wx.MessageBox(
                    "This image produces %d polygons and may make KiCad very slow.\n"
                    "A smaller size or Threshold mode gives fewer polygons.\n\nContinue?"
                    % len(rects), "Img2Silk", wx.YES_NO | wx.ICON_WARNING) != wx.YES:
                return

            xs = [pcbnew.FromMM(x0 + i * length / w_px) for i in range(w_px + 1)]
            ys = [pcbnew.FromMM(y0 + i * width / h_px) for i in range(h_px + 1)]
            group = pcbnew.PCB_GROUP(self.board)
            group.SetName("Img2Silk")
            self.board.Add(group)
            for i in range(0, len(rects), 2000):
                poly = pcbnew.SHAPE_POLY_SET()
                for xa, xb, ya, yb in rects[i:i + 2000]:
                    chain = pcbnew.SHAPE_LINE_CHAIN()
                    chain.Append(xs[xa], ys[ya])
                    chain.Append(xs[xb], ys[ya])
                    chain.Append(xs[xb], ys[yb])
                    chain.Append(xs[xa], ys[yb])
                    chain.SetClosed(True)
                    poly.AddOutline(chain)
                shape = pcbnew.PCB_SHAPE(self.board)
                shape.SetShape(pcbnew.SHAPE_T_POLY)
                shape.SetFilled(True)
                shape.SetLayer((pcbnew.F_Cu, pcbnew.F_SilkS,
                                pcbnew.F_Mask)[self.layer.GetSelection()])
                shape.SetPolyShape(poly)
                shape.SetWidth(0)
                self.board.Add(shape)
                group.AddItem(shape)
            pcbnew.Refresh()
            self.EndModal(wx.ID_OK)


if pcbnew:

    class Img2Silk(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "Img2Silk"
            self.category = "Graphics"
            self.description = "Import a JPG/PNG image as silkscreen/copper/mask graphics"
            self.show_toolbar_button = True
            self.icon_file_name = os.path.join(os.path.dirname(__file__),
                                               "assets", "icon.png")

        def Run(self):
            try:
                wx.InitAllImageHandlers()
            except Exception:
                pass
            dlg = Img2SilkDialog(wx.GetActiveWindow(), pcbnew.GetBoard())
            dlg.ShowModal()
            dlg.Destroy()


if __name__ == "__main__":
    assert _runs([]) == []
    assert _runs([0, 0]) == []
    assert _runs([1, 1]) == [(0, 2)]
    assert _runs([0, 1, 1, 0, 1]) == [(1, 3), (4, 5)]
    assert list(_dither(bytes([0, 0, 0, 255, 255, 255]), 2, 1, None, 128, False, 0)) == [1, 0]
    assert list(_dither(bytes([0, 0, 0, 255, 255, 255]), 2, 1, None, 128, True, 0)) == [0, 1]
    assert list(_dither(bytes([0, 0, 0]), 1, 1, bytes([0]), 128, False, 0)) == [0]
    assert list(_dither(bytes([50, 50, 50, 120, 120, 120]), 2, 1,
                        bytes([0, 255]), 128, False, 1)) == [0, 1]
    grey100 = bytes([128, 128, 128] * 100)
    for algo in (1, 2, 3, 4):
        cov = sum(_dither(grey100, 10, 10, None, 128, False, algo))
        assert 25 <= cov <= 75, (algo, cov)
    assert _dither(grey100, 10, 10, None, 128, False, 4) == \
        _dither(grey100, 10, 10, None, 128, False, 4)
    assert _rects(bytearray([1, 1, 1, 1]), 2, 2) == [(0, 2, 0, 2)]
    assert sorted(_rects(bytearray([1, 0, 0, 1]), 2, 2)) == [(0, 1, 0, 1), (1, 2, 1, 2)]
    print("self-check ok")
