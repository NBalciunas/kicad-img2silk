import os

try:
    import pcbnew
    import wx
except ImportError:
    pcbnew = wx = None

MAX_PX = 1000
PX_PER_MM = 10.0


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


if wx:

    class Img2SilkDialog(wx.Dialog):
        def __init__(self, parent, board):
            super().__init__(parent, title="Img2Silk")
            self.board = board
            self.image = None

            s = wx.BoxSizer(wx.VERTICAL)
            title = wx.StaticText(self, label="Img2Silk v1.0")
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

            grid = wx.FlexGridSizer(5, 2, 5, 5)
            grid.AddGrowableCol(1)
            grid.Add(wx.StaticText(self, label="Black / white threshold:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.threshold = wx.Slider(self, value=50, minValue=0, maxValue=100,
                                       style=wx.SL_HORIZONTAL | wx.SL_LABELS)
            self.threshold.Disable()
            self.threshold.Bind(wx.EVT_SLIDER, self.update_bw)
            grid.Add(self.threshold, 1, wx.EXPAND)
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

            btn_create = wx.Button(self, label="Create Silkscreen")
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
            self.threshold.Enable()
            self.invert.Enable()
            self.length.Enable()
            self.width.Enable()
            self.keep_aspect.Enable()
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
            if self.image is not None and self.keep_aspect.GetValue():
                v = self._mm(self.length)
                if v:
                    self.width.ChangeValue("%.2f" % (v * self._aspect))

        def on_width(self, _):
            if self.image is not None and self.keep_aspect.GetValue():
                v = self._mm(self.width)
                if v:
                    self.length.ChangeValue("%.2f" % (v / self._aspect))

        def on_keep_aspect(self, _):
            if self.keep_aspect.GetValue():
                self.on_length(None)

        def update_bw(self, _=None):
            if self.image is None:
                return
            thresh = self.threshold.GetValue() * 255 // 100
            inv = self.invert.GetValue()
            g, a = self._grey, self._alpha
            out = bytearray(len(g))
            for i in range(0, len(g), 3):
                on = (g[i] < thresh) != inv
                if a is not None and a[i // 3] < 128:
                    on = False
                out[i] = out[i + 1] = out[i + 2] = 0 if on else 255
            bw = wx.Image(self._pw, self._ph, bytes(out))
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

            img = self.image.ConvertToGreyscale()
            w_px = int(min(img.GetWidth(), max(16, length * PX_PER_MM), MAX_PX))
            h_px = int(min(img.GetHeight(), max(16, width * PX_PER_MM), MAX_PX))
            img.Rescale(w_px, h_px, wx.IMAGE_QUALITY_HIGH)
            thresh = self.threshold.GetValue() * 255 // 100
            inv = self.invert.GetValue()
            has_alpha = img.HasAlpha()

            def dark(x, y):
                if has_alpha and img.GetAlpha(x, y) < 128:
                    return False
                return (img.GetRed(x, y) < thresh) != inv

            bbox = self.board.GetBoardEdgesBoundingBox()
            if bbox.GetWidth() > 0 and bbox.GetHeight() > 0:
                cx = pcbnew.ToMM(bbox.GetCenter().x)
                cy = pcbnew.ToMM(bbox.GetCenter().y)
            else:
                cx, cy = 100.0, 100.0
            x0, y0 = cx - length / 2.0, cy - width / 2.0

            poly = pcbnew.SHAPE_POLY_SET()
            count = 0
            for y in range(h_px):
                row = [dark(x, y) for x in range(w_px)]
                for xa, xb in _runs(row):
                    x1 = pcbnew.FromMM(x0 + xa * length / w_px)
                    x2 = pcbnew.FromMM(x0 + xb * length / w_px)
                    y1 = pcbnew.FromMM(y0 + y * width / h_px)
                    y2 = pcbnew.FromMM(y0 + (y + 1) * width / h_px)
                    chain = pcbnew.SHAPE_LINE_CHAIN()
                    for px, py in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
                        chain.Append(px, py)
                    chain.SetClosed(True)
                    poly.AddOutline(chain)
                    count += 1
            if count == 0:
                wx.MessageBox("No pixels selected at this threshold. Try moving the slider.",
                              "Img2Silk", wx.ICON_WARNING)
                return
            try:
                poly.Simplify(pcbnew.SHAPE_POLY_SET.PM_FAST)
            except (TypeError, AttributeError):
                poly.Simplify()

            shape = pcbnew.PCB_SHAPE(self.board)
            shape.SetShape(pcbnew.SHAPE_T_POLY)
            shape.SetFilled(True)
            shape.SetLayer(pcbnew.F_SilkS)
            shape.SetPolyShape(poly)
            shape.SetWidth(0)
            self.board.Add(shape)
            pcbnew.Refresh()
            self.EndModal(wx.ID_OK)


if pcbnew:

    class Img2Silk(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "Img2Silk"
            self.category = "Graphics"
            self.description = "Import a JPG/PNG image as silkscreen graphics"
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
    print("self-check ok")
