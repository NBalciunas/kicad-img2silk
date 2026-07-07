import json
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
    out, active = [], {}
    for y in range(h + 1):
        runs = set(_runs(on[y * w:(y + 1) * w])) if y < h else set()
        for r in [k for k in active if k not in runs]:
            out.append((r[0], r[1], active.pop(r), y))
        for r in runs:
            active.setdefault(r, y)
    return out


def _outline(on, w, h):
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            i = y * w + x
            if on[i] and (x == 0 or x == w - 1 or y == 0 or y == h - 1
                          or not (on[i - 1] and on[i + 1]
                                  and on[i - w] and on[i + w])):
                out[i] = 1
    return out


_MASKS = (("Black", (25, 25, 25), (60, 55, 50)),
          ("Blue", (15, 35, 90), (60, 90, 160)),
          ("Green", (15, 70, 40), (60, 130, 75)),
          ("Purple", (60, 25, 90), (110, 70, 150)),
          ("Red", (120, 20, 25), (180, 70, 60)),
          ("White", (225, 225, 225), (235, 230, 218)),
          ("Yellow", (200, 160, 30), (215, 180, 60)))
_FINISH = (("ENIG (Gold)", (225, 185, 95)), ("HASL (Silver)", (200, 200, 200)))
_DEF_MASK = 2
_DEF_FINISH = 1
_LAYER_CHOICES = ("Copper", "Silkscreen", "Solder Mask", "Copper and Solder Mask")
_LAYER_MAP = ((0,), (1,), (2,), (0, 2))
_FR4 = (205, 180, 140)
_SILK_WHITE = (250, 250, 250)


def _lum(rgb):
    return (299 * rgb[0] + 587 * rgb[1] + 114 * rgb[2]) // 1000


def _palette5(mask_idx, fin_idx, mode=0):
    _, m0, m1 = _MASKS[mask_idx]
    pal = [(m0, (0, 0, 0)), (_FR4, (0, 1, 0)), (_SILK_WHITE, (0, 0, 1))]
    if mode == 0:
        pal += [(m1, (1, 0, 0)), (_FINISH[fin_idx][1], (1, 1, 0))]
    pal.sort(key=lambda p: _lum(p[0]))
    return pal


def _dither5(grey, w, h, alpha, lums, blank, bias, inv, algo):
    n = w * h
    tbl = bytes(min(range(len(lums)), key=lambda k: abs(lums[k] - v))
                for v in range(256))
    amp = (lums[-1] - lums[0]) // (2 * (len(lums) - 1)) or 1

    def adj(v):
        v = (255 - v if inv else v) + bias
        return 0 if v < 0 else 255 if v > 255 else v

    if algo in _KERNELS:
        div, kernel = _KERNELS[algo]
        px = [float(adj(grey[i * 3])) for i in range(n)]
        if alpha is not None:
            for i in range(n):
                if alpha[i] < 128:
                    px[i] = float(lums[blank])
        lv = bytearray(n)
        i = 0
        for y in range(h):
            for x in range(w):
                old = px[i]
                c = tbl[0 if old < 0 else 255 if old > 255 else int(old)]
                lv[i] = c
                err = (old - lums[c]) / div
                for dx, dy, wt in kernel:
                    xx = x + dx
                    if 0 <= xx < w and y + dy < h:
                        px[i + dy * w + dx] += err * wt
                i += 1
    elif algo == 3:
        lv = bytearray(n)
        i = 0
        for y in range(h):
            brow = _BAYER[y & 7]
            for x in range(w):
                v = adj(grey[i * 3]) + (brow[x & 7] * amp) // 32 - amp
                lv[i] = tbl[0 if v < 0 else 255 if v > 255 else v]
                i += 1
    elif algo == 4:
        rnd = random.Random(0)
        lv = bytearray(n)
        for i in range(n):
            v = adj(grey[i * 3]) + rnd.randrange(-amp, amp + 1)
            lv[i] = tbl[0 if v < 0 else 255 if v > 255 else v]
    else:
        lv = bytearray(grey[::3].translate(
            bytes(tbl[adj(v)] for v in range(256))))
    if alpha is not None:
        for i in range(n):
            if alpha[i] < 128:
                lv[i] = blank
    return lv


if wx:

    _GRAVEYARD = []

    class Img2SilkDialog(wx.Dialog):
        def __init__(self, parent, board):
            super().__init__(parent, title="Img2Silk")
            self.board = board
            self.image = None
            self.replace = None
            self.center = None
            self.ref_uuid = None
            self._ref_geom = None
            self._ref_good = None
            self._ref_shown = None
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
            self.timer.Start(300)

            s = wx.BoxSizer(wx.VERTICAL)
            title = wx.StaticText(self, label="Img2Silk v1.3")
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

            grid = wx.FlexGridSizer(14, 2, 5, 5)
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
            self.layer = wx.Choice(self, choices=list(_LAYER_CHOICES))
            self.layer.SetSelection(1)
            self.layer.Disable()
            grid.Add(self.layer, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Side:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.side = wx.Choice(self, choices=["Front", "Back"])
            self.side.SetToolTip("Back side graphics are mirrored so they "
                                 "read correctly viewed from the back.")
            self.side.SetSelection(0)
            self.side.Disable()
            grid.Add(self.side, 1, wx.EXPAND)
            self.outline = wx.CheckBox(self, label="Toggle outline")
            self.outline.Disable()
            self.outline.Bind(wx.EVT_CHECKBOX, self.on_outline)
            grid.Add(self.outline, 0, wx.ALIGN_CENTER_VERTICAL)
            self.outline_layer = wx.Choice(self, choices=list(_LAYER_CHOICES))
            self.outline_layer.SetSelection(1)
            self.outline_layer.Disable()
            self.outline_layer.Bind(wx.EVT_CHOICE, self.update_bw)
            grid.Add(self.outline_layer, 1, wx.EXPAND)
            self.five = wx.CheckBox(self, label="Multi-color graphic")
            self.five.SetToolTip(
                "Dithers the image using the board itself as a palette. "
                "Shades are built by stacking silkscreen, copper, and solder "
                "mask openings.")
            self.five.Disable()
            self.five.Bind(wx.EVT_CHECKBOX, self.on_5c)
            grid.Add(self.five, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add((0, 0))
            grid.Add(wx.StaticText(self, label="Multi-color PCB layers:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.mc_layers = wx.Choice(self, choices=[
                "Silkscreen, Copper and Solder Mask (5 Colors)",
                "Silkscreen and Solder Mask (3 Colors)"])
            self.mc_layers.SetSelection(0)
            self.mc_layers.Disable()
            self.mc_layers.Bind(wx.EVT_CHOICE, self.on_5c_layers)
            grid.Add(self.mc_layers, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="PCB color:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.mask_col = wx.Choice(self, choices=[m[0] for m in _MASKS])
            self.mask_col.SetSelection(_DEF_MASK)
            self.mask_col.Disable()
            self.mask_col.Bind(wx.EVT_CHOICE, self.update_bw)
            grid.Add(self.mask_col, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self, label="Surface finish:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.finish = wx.Choice(self, choices=[f[0] for f in _FINISH])
            self.finish.SetSelection(_DEF_FINISH)
            self.finish.Disable()
            self.finish.Bind(wx.EVT_CHOICE, self.update_bw)
            grid.Add(self.finish, 1, wx.EXPAND)
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

            btn_ref = wx.Button(self, label="Place Reference Frame")
            btn_ref.SetToolTip(
                "Adds a rectangle to the board on the Dwgs.User layer. Move "
                "and resize it like any other graphic, then click Insert "
                "Graphics to fit the image into it. The frame is removed "
                "automatically.")
            btn_ref.Bind(wx.EVT_BUTTON, self.on_place_ref)
            s.Add(btn_ref, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)

            self.btn_create = wx.Button(self, label="Insert Graphics")
            self.btn_create.Bind(wx.EVT_BUTTON, self.on_create)
            s.Add(self.btn_create, 0, wx.ALL | wx.EXPAND, 10)

            self.SetSizerAndFit(s)
            self.CentreOnParent()

            wx.CallAfter(self._detect_group)

        def _detect_group(self):
            groups = [g for g in self.board.Groups()
                      if g.GetName().startswith("Img2Silk|")]
            uids = {g.m_Uuid.AsString(): g for g in groups}
            target = next((g for g in groups if g.IsSelected()), None)
            if target is None:
                for d in self.board.GetDrawings():
                    pg = d.GetParentGroup()
                    if pg and d.IsSelected() and pg.m_Uuid.AsString() in uids:
                        target = uids[pg.m_Uuid.AsString()]
                        break
            if target is not None:
                self._load_group(target)
            elif not groups and any(g.GetName() == "Img2Silk" and g.IsSelected()
                                    for g in self.board.Groups()):
                wx.MessageBox("This graphic was placed by an older Img2Silk "
                              "version, so its settings were not saved and it "
                              "cannot be edited.\nDelete it and insert a new "
                              "one.", "Img2Silk", wx.ICON_INFORMATION, self)

        def _load_group(self, group):
            try:
                cfg = json.loads(group.GetName().split("|", 1)[1])
                if not self._load(cfg["img"]):
                    raise ValueError("image not found: %s" % cfg["img"])
            except (ValueError, KeyError, TypeError) as e:
                wx.MessageBox("Could not reload the selected graphic's source "
                              "image.\n%s" % e, "Img2Silk", wx.ICON_WARNING)
                return
            self.replace = group.m_Uuid.AsString()
            c = group.GetBoundingBox().GetCenter()
            self.center = (pcbnew.ToMM(c.x), pcbnew.ToMM(c.y))
            self.dither.SetSelection(cfg["algo"])
            self.threshold.SetValue(cfg["thr"])
            self.dot_size.SetValue(cfg["dot"])
            self.dot_val.SetLabel("%.2f" % (cfg["dot"] / 100.0))
            self.invert.SetValue(cfg["inv"])
            self.layer.SetSelection(cfg["layer"])
            self.side.SetSelection(cfg.get("side", 0))
            self.outline.SetValue(cfg.get("ol", False))
            self.outline_layer.SetSelection(cfg.get("olay", 1))
            self.outline_layer.Enable(self.outline.GetValue())
            self.five.SetValue(cfg.get("5c", False))
            self.mc_layers.SetSelection(cfg.get("mcl", 0))
            self.mask_col.SetSelection(cfg.get("mask", _DEF_MASK))
            self.finish.SetSelection(cfg.get("fin", _DEF_FINISH))
            self.length.ChangeValue("%g" % cfg["len"])
            self.width.ChangeValue("%g" % cfg["wid"])
            self.btn_create.SetLabel("Update Graphics")
            self.on_5c(None)

        def on_import(self, _):
            with wx.FileDialog(self, "Choose an image",
                               wildcard="Images (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg",
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fd:
                if fd.ShowModal() != wx.ID_OK:
                    return
                path = fd.GetPath()
            if not self._load(path):
                wx.MessageBox("Could not load image.", "Img2Silk", wx.ICON_ERROR)

        def _load(self, path):
            nolog = wx.LogNull()
            img = wx.Image(path)
            del nolog
            if not img.IsOk():
                return False
            self.img_path = path
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
            self.side.Enable()
            self.outline.Enable()
            self.outline_layer.Enable(self.outline.GetValue())
            self.five.Enable()
            self.length.ChangeValue("50")
            self.width.ChangeValue("%.2f" % (50.0 * self._aspect))
            self.update_bw()
            self.Fit()
            self.Layout()
            return True

        def _board_center(self):
            if self.center:
                return self.center
            bbox = self.board.GetBoardEdgesBoundingBox()
            if bbox.GetWidth() > 0 and bbox.GetHeight() > 0:
                return pcbnew.ToMM(bbox.GetCenter().x), pcbnew.ToMM(bbox.GetCenter().y)
            return 100.0, 100.0

        def _remove(self, item):
            getattr(item, "ClearSelected", lambda: None)()
            _GRAVEYARD.append(item)
            self.board.Remove(item)

        def _find_ref(self):
            if self.ref_uuid:
                for d in self.board.GetDrawings():
                    if d.m_Uuid.AsString() == self.ref_uuid:
                        return d
            return None

        def _del_ref(self):
            ref = self._find_ref()
            self.ref_uuid = None
            self._ref_geom = None
            if ref is not None:
                self._remove(ref)
            return ref is not None

        def on_tick(self, _):
            ref = self._find_ref()
            if ref is None:
                return
            p0, p1 = ref.GetStart(), ref.GetEnd()
            geom = (p0.x, p0.y, p1.x, p1.y)
            moving = geom != self._ref_geom
            self._ref_geom = geom
            length = abs(pcbnew.ToMM(p1.x - p0.x))
            width = abs(pcbnew.ToMM(p1.y - p0.y))
            if moving:
                self.length.ChangeValue("%.2f" % length)
                self.width.ChangeValue("%.2f" % width)
                return
            if (self.image is not None and self.keep_aspect.GetValue()
                    and length > 0 and abs(width - length * self._aspect) > 0.01):
                gl, gw = self._ref_good or (length, width)
                if abs(width - gw) > abs(length - gl):
                    length = width / self._aspect
                    ref.SetEnd(pcbnew.VECTOR2I(
                        p0.x + (1 if p1.x >= p0.x else -1) * pcbnew.FromMM(length),
                        p1.y))
                else:
                    width = length * self._aspect
                    ref.SetEnd(pcbnew.VECTOR2I(
                        p1.x,
                        p0.y + (1 if p1.y >= p0.y else -1) * pcbnew.FromMM(width)))
                self._ref_geom = None
                pcbnew.Refresh()
            self._ref_good = (length, width)
            if (length, width) != self._ref_shown:
                self._ref_shown = (length, width)
                self.length.ChangeValue("%.2f" % length)
                self.width.ChangeValue("%.2f" % width)
                self.update_bw()

        def on_place_ref(self, _):
            self._del_ref()
            length = self._mm(self.length) or 50.0
            width = self._mm(self.width) or 50.0
            cx, cy = self._board_center()
            rect = pcbnew.PCB_SHAPE(self.board)
            rect.SetShape(pcbnew.SHAPE_T_RECT)
            rect.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(cx - length / 2.0),
                                          pcbnew.FromMM(cy - width / 2.0)))
            rect.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(cx + length / 2.0),
                                        pcbnew.FromMM(cy + width / 2.0)))
            rect.SetLayer(pcbnew.Dwgs_User)
            rect.SetWidth(pcbnew.FromMM(0.2))
            self.board.Add(rect)
            self.ref_uuid = rect.m_Uuid.AsString()
            self._ref_geom = None
            self._ref_good = (length, width)
            self._ref_shown = (length, width)
            pcbnew.Refresh()

        def on_close(self, _):
            self.timer.Stop()
            if self._del_ref():
                pcbnew.Refresh()
            self.Destroy()

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

        def on_outline(self, _):
            self.outline_layer.Enable(self.outline.GetValue())
            self.update_bw()

        def on_5c_layers(self, _):
            self.finish.Enable(self.five.GetValue()
                               and self.mc_layers.GetSelection() == 0)
            self.update_bw()

        def on_5c(self, _):
            f = self.five.GetValue()
            self.mc_layers.Enable(f)
            self.mask_col.Enable(f)
            self.finish.Enable(f and self.mc_layers.GetSelection() == 0)
            self.layer.Enable(not f)
            self.outline.Enable(not f)
            self.outline_layer.Enable(not f and self.outline.GetValue())
            self.update_bw()

        def on_dot_size(self, _):
            self.dot_val.SetLabel("%.2f" % (self.dot_size.GetValue() / 100.0))
            self.update_bw()

        def _out_grid(self, dotted, length, width):
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
            if self.five.GetValue():
                pal, lv = self._levels5(grey, alpha, w_px, h_px)
                out = bytearray(len(lv) * 3)
                for ch in range(3):
                    out[ch::3] = lv.translate(
                        bytes(p[0][ch] for p in pal).ljust(256, b"\x00"))
            else:
                on = _dither(grey, w_px, h_px, alpha,
                             self.threshold.GetValue() * 255 // 100,
                             self.invert.GetValue(), algo)
                v = bytes(on).translate(bytes([255] + [0] * 255))
                out = bytearray(len(v) * 3)
                out[0::3] = out[1::3] = out[2::3] = v
                if self.outline.GetValue():
                    ol = _outline(bytes(b ^ 1 for b in on), w_px, h_px)
                    for i, o in enumerate(ol):
                        if o:
                            out[i * 3 + 1] = out[i * 3 + 2] = 0
            bw = wx.Image(w_px, h_px, bytes(out))
            k = min(280.0 / w_px, 280.0 / h_px)
            bw.Rescale(max(1, int(w_px * k)), max(1, int(h_px * k)),
                       wx.IMAGE_QUALITY_HIGH if k < 1 else wx.IMAGE_QUALITY_NORMAL)
            self.preview_bw.SetBitmap(wx.Bitmap(bw))

        def _levels5(self, grey, alpha, w_px, h_px):
            pal = _palette5(self.mask_col.GetSelection(),
                            self.finish.GetSelection(),
                            self.mc_layers.GetSelection())
            lv = _dither5(grey, w_px, h_px, alpha,
                          [_lum(p[0]) for p in pal],
                          next(i for i, p in enumerate(pal)
                               if p[1] == (0, 0, 0)),
                          (50 - self.threshold.GetValue()) * 255 // 100,
                          self.invert.GetValue(),
                          _ALGOS[self.dither.GetSelection()][1])
            return pal, bytes(lv)

        def on_create(self, _):
            if self.image is None:
                wx.MessageBox("Import an image first.", "Img2Silk", wx.ICON_WARNING)
                return
            ref = self._find_ref()
            if ref is not None:
                p0, p1 = ref.GetStart(), ref.GetEnd()
                length = abs(pcbnew.ToMM(p1.x - p0.x))
                width = abs(pcbnew.ToMM(p1.y - p0.y))
                cx = pcbnew.ToMM((p0.x + p1.x) // 2)
                cy = pcbnew.ToMM((p0.y + p1.y) // 2)
                if length <= 0 or width <= 0:
                    wx.MessageBox("The reference frame has zero size.",
                                  "Img2Silk", wx.ICON_WARNING)
                    return
            else:
                try:
                    length = float(self.length.GetValue())
                    width = float(self.width.GetValue())
                    if length <= 0 or width <= 0:
                        raise ValueError
                except ValueError:
                    wx.MessageBox("Length and Width must be positive numbers.",
                                  "Img2Silk", wx.ICON_WARNING)
                    return
                cx, cy = self._board_center()

            algo = _ALGOS[self.dither.GetSelection()][1]
            busy = wx.BusyCursor()
            w_px, h_px = self._out_grid(algo != 0, length, width)
            img = self.image.ConvertToGreyscale()
            img.Rescale(w_px, h_px, wx.IMAGE_QUALITY_HIGH)
            grey = bytes(img.GetData())
            alpha = bytes(img.GetAlphaBuffer()) if img.HasAlpha() else None
            back = self.side.GetSelection() == 1
            layers = ((pcbnew.B_Cu, pcbnew.B_SilkS, pcbnew.B_Mask) if back
                      else (pcbnew.F_Cu, pcbnew.F_SilkS, pcbnew.F_Mask))
            if self.five.GetValue():
                pal, lv = self._levels5(grey, alpha, w_px, h_px)
                jobs = [(layer, _rects(lv.translate(
                            bytes(p[1][k] for p in pal).ljust(256, b"\x00")),
                            w_px, h_px))
                        for k, layer in ((0, layers[0]), (1, layers[2]),
                                         (2, layers[1]))]
            else:
                on = _dither(grey, w_px, h_px, alpha,
                             self.threshold.GetValue() * 255 // 100,
                             not self.invert.GetValue(), algo)
                img_rects = _rects(on, w_px, h_px)
                jobs = [(layers[k], img_rects)
                        for k in _LAYER_MAP[self.layer.GetSelection()]]
                if self.outline.GetValue():
                    ol_rects = _rects(_outline(on, w_px, h_px), w_px, h_px)
                    jobs += [(layers[k], ol_rects)
                             for k in _LAYER_MAP[self.outline_layer.GetSelection()]]

            x0, y0 = cx - length / 2.0, cy - width / 2.0

            total = sum(len(r) for _, r in jobs)
            if total == 0:
                wx.MessageBox("No pixels selected at this threshold. Try moving the slider.",
                              "Img2Silk", wx.ICON_WARNING)
                return
            if total > 30000 and wx.MessageBox(
                    "This image produces %d polygons and may make KiCad very slow.\n"
                    "A smaller size or Threshold mode gives fewer polygons.\n\nContinue?"
                    % total, "Img2Silk", wx.YES_NO | wx.ICON_WARNING) != wx.YES:
                return

            self._del_ref()
            if self.replace:
                old = [d for d in self.board.GetDrawings()
                       if d.GetParentGroup()
                       and d.GetParentGroup().m_Uuid.AsString() == self.replace]
                for d in old:
                    self._remove(d)
                for g in self.board.Groups():
                    if g.m_Uuid.AsString() == self.replace:
                        self._remove(g)
                        break

            xs = [pcbnew.FromMM(x0 + i * length / w_px) for i in range(w_px + 1)]
            ys = [pcbnew.FromMM(y0 + i * width / h_px) for i in range(h_px + 1)]
            if back:
                xs.reverse()
            group = pcbnew.PCB_GROUP(self.board)
            group.SetName("Img2Silk|" + json.dumps(
                {"img": self.img_path, "algo": self.dither.GetSelection(),
                 "thr": self.threshold.GetValue(), "dot": self.dot_size.GetValue(),
                 "inv": self.invert.GetValue(), "layer": self.layer.GetSelection(),
                 "side": self.side.GetSelection(),
                 "ol": self.outline.GetValue(),
                 "olay": self.outline_layer.GetSelection(),
                 "5c": self.five.GetValue(),
                 "mcl": self.mc_layers.GetSelection(),
                 "mask": self.mask_col.GetSelection(),
                 "fin": self.finish.GetSelection(),
                 "len": length, "wid": width}))
            self.board.Add(group)
            for layer, rects in jobs:
                if rects:
                    self._add_polys(group, rects, xs, ys, layer)
            pcbnew.Refresh()
            self.Close()

        def _add_polys(self, group, rects, xs, ys, layer):
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
                shape.SetLayer(layer)
                shape.SetPolyShape(poly)
                shape.SetWidth(0)
                self.board.Add(shape)
                group.AddItem(shape)


if pcbnew:

    class Img2Silk(pcbnew.ActionPlugin):
        _dlg = None

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
            if Img2Silk._dlg is not None:
                try:
                    Img2Silk._dlg.Raise()
                    return
                except RuntimeError:
                    pass
            Img2Silk._dlg = Img2SilkDialog(wx.GetActiveWindow(), pcbnew.GetBoard())
            Img2Silk._dlg.Show()


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
    assert list(_outline(bytearray([1] * 9), 3, 3)) == [1, 1, 1, 1, 0, 1, 1, 1, 1]
    assert list(_outline(bytearray([0, 1, 0, 1, 1, 1, 0, 1, 0]), 3, 3)) == \
        [0, 1, 0, 1, 0, 1, 0, 1, 0]
    assert list(_outline(bytearray(9), 3, 3)) == [0] * 9
    lums5 = [0, 64, 128, 192, 255]
    ramp = bytes(b for v in (0, 64, 128, 192, 255) for b in (v, v, v))
    assert list(_dither5(ramp, 5, 1, None, lums5, 0, 0, False, 0)) == [0, 1, 2, 3, 4]
    assert list(_dither5(ramp, 5, 1, None, lums5, 0, 0, True, 0)) == [4, 3, 2, 1, 0]
    assert list(_dither5(ramp, 5, 1, bytes([255, 0, 255, 0, 255]),
                         lums5, 0, 0, False, 0)) == [0, 0, 2, 0, 4]
    assert list(_dither5(ramp, 5, 1, None, lums5, 0, -255, False, 0)) == [0] * 5
    g96 = bytes([96, 96, 96] * 100)
    for algo in (0, 1, 2, 3, 4):
        lv = _dither5(g96, 10, 10, None, lums5, 0, 0, False, algo)
        avg = sum(lums5[c] for c in lv) / 100.0
        assert 60 <= avg <= 132, (algo, avg)
    lums3 = [0, 128, 255]
    assert list(_dither5(ramp, 5, 1, None, lums3, 0, 0, False, 0)) == [0, 0, 1, 2, 2]
    for algo in (1, 3, 4):
        lv = _dither5(g96, 10, 10, None, lums3, 0, 0, False, algo)
        avg = sum(lums3[c] for c in lv) / 100.0
        assert 60 <= avg <= 132, (algo, avg)
    for m in range(len(_MASKS)):
        pal = _palette5(m, 0, 1)
        assert {p[1] for p in pal} == {(0, 0, 0), (0, 1, 0), (0, 0, 1)}
        for f in range(len(_FINISH)):
            pal = _palette5(m, f)
            assert {p[1] for p in pal} == {(0, 0, 0), (1, 0, 0), (0, 1, 0),
                                           (1, 1, 0), (0, 0, 1)}
            assert [_lum(p[0]) for p in pal] == sorted(_lum(p[0]) for p in pal)
    assert _rects(bytearray([1, 1, 1, 1]), 2, 2) == [(0, 2, 0, 2)]
    assert sorted(_rects(bytearray([1, 0, 0, 1]), 2, 2)) == [(0, 1, 0, 1), (1, 2, 1, 2)]
    print("self-check ok")
