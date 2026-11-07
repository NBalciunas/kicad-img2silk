import bisect
import math
import sys

try:
    import pcbnew
except ImportError:
    pcbnew = None

# This module makes traces that fill a shape. Adjacent runs are always a
# minimum of one pitch (track width + gap) apart. A trace does not cross itself.
#
# Follow outline: The module traces the outline in 45 degree steps. Then it
# offsets the outline into rings that are one pitch apart. A port cuts a stack
# of rings where the rings are straight. Each net connects a band of these rings
# in a back-and-forth sequence. The net goes out of the shape at its two ends.
# Other rings connect in the same way through thin slots. This method comes from
# "connected Fermat spirals" (Zhao et al., SIGGRAPH 2016). Parts that are too
# thin for rings become thin branch lines. Each branch line ends in a via.
#
# Rows: A grid of cells covers the shape. Each cell is two pitches wide. The
# trace goes around a spanning tree of the cells. The tree connects the cells
# row by row. This gives back-and-forth rows, as in a heater meander.

_SIDES = ((0, -1), (1, 0), (0, 1), (-1, 0))
# This is the maximum distance between the traced outline and the image, in pitches.
TRACE_TOL = 0.25
_DIRS = tuple((math.cos(k * math.pi / 4), math.sin(k * math.pi / 4))
              for k in range(8))


# Rows

def _component(cells):
    # Find the largest 4-connected group of cells. One trace cannot go between
    # separate islands. Thus, ignore the smaller groups.
    left, best = set(cells), set()
    while left:
        seed = left.pop()
        comp, todo = {seed}, [seed]
        while todo:
            x, y = todo.pop()
            for dx, dy in _SIDES:
                n = (x + dx, y + dy)
                if n in left:
                    left.discard(n)
                    comp.add(n)
                    todo.append(n)
        if len(comp) > len(best):
            best = comp
    return best


def _grid_tree(cells):
    edges = sorted((a, (a[0] + dx, a[1] + dy)) for a in cells
                   for dx, dy in ((1, 0), (0, 1))
                   if (a[0] + dx, a[1] + dy) in cells)
    root = {c: c for c in cells}

    def find(c):
        while root[c] != c:
            root[c] = root[root[c]]
            c = root[c]
        return c

    tree = set()
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            root[ra] = rb
            tree.add((a, b))
            tree.add((b, a))
    return tree


def rows(cells):
    # Get the half-cell grid points of one open trace that covers the cells.
    # If there are no cells to cover, return [].
    cells = _component(cells)
    if not cells:
        return []
    tree = _grid_tree(cells)
    link = {}

    def join(a, b):
        link.setdefault(a, []).append(b)
        link.setdefault(b, []).append(a)

    for x, y in cells:
        sx, sy = 2 * x, 2 * y
        # Each side of a cell opens into a tree neighbour or a run closes it.
        # If the side opens, the two half cells adjacent to it go across. The
        # left cell or the top cell controls the shared sides.
        if ((x, y), (x + 1, y)) in tree:
            join((sx + 1, sy), (sx + 2, sy))
            join((sx + 1, sy + 1), (sx + 2, sy + 1))
        else:
            join((sx + 1, sy), (sx + 1, sy + 1))
        if ((x, y), (x, y + 1)) in tree:
            join((sx, sy + 1), (sx, sy + 2))
            join((sx + 1, sy + 1), (sx + 1, sy + 2))
        else:
            join((sx, sy + 1), (sx + 1, sy + 1))
        if ((x - 1, y), (x, y)) not in tree:
            join((sx, sy), (sx, sy + 1))
        if ((x, y - 1), (x, y)) not in tree:
            join((sx, sy), (sx + 1, sy))
    # Cut the loop along the top side of the top-left cell.
    x, y = min(cells, key=lambda c: (c[1], c[0]))
    a, b = (2 * x, 2 * y), (2 * x + 1, 2 * y)
    link[a].remove(b)
    link[b].remove(a)
    out = [a]
    prev, cur = None, a
    while cur != b:
        nxt = link[cur][0] if link[cur][0] != prev else link[cur][-1]
        prev, cur = cur, nxt
        out.append(cur)
    return out


def chamfer(pts, size):
    # Remove the points in the middle of straight runs. Cut each right-angle
    # corner with a 45 degree chamfer of the given leg length. The leg length is
    # a maximum of half of each adjacent run. On a grid path with a chamfer of half
    # the grid step, a staircase becomes one straight diagonal run.
    keep = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = keep[-1], pts[i], pts[i + 1]
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-9 \
                or (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) < 0:
            keep.append(b)
    keep.append(pts[-1])
    out = [keep[0]]
    for i in range(1, len(keep) - 1):
        a, b, c = keep[i - 1], keep[i], keep[i + 1]
        u = (b[0] - a[0], b[1] - a[1])
        v = (c[0] - b[0], c[1] - b[1])
        lu, lv = math.hypot(*u), math.hypot(*v)
        if size <= 0 or abs(u[0] * v[0] + u[1] * v[1]) > 1e-6 * lu * lv:
            out.append(b)
            continue
        leg = min(size, lu / 2, lv / 2)
        out.append((b[0] - u[0] * leg / lu, b[1] - u[1] * leg / lu))
        out.append((b[0] + v[0] * leg / lv, b[1] + v[1] * leg / lv))
    out.append(keep[-1])
    return _dedupe(out)


def _dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    return out


# Outlines

def loops(on, w, h):
    # Get the pixel-edge outlines of the set pixels, in pixel corner coordinates.
    # Each step keeps the set pixels on its right side (the y axis points down).
    # Thus, outer outlines have a positive area and holes have a negative area.
    nxt = {}
    for y in range(h):
        row = y * w
        for x in range(w):
            if not on[row + x]:
                continue
            if y == 0 or not on[row - w + x]:
                nxt.setdefault((x, y), []).append((x + 1, y))
            if x == w - 1 or not on[row + x + 1]:
                nxt.setdefault((x + 1, y), []).append((x + 1, y + 1))
            if y == h - 1 or not on[row + w + x]:
                nxt.setdefault((x + 1, y + 1), []).append((x, y + 1))
            if x == 0 or not on[row + x - 1]:
                nxt.setdefault((x, y + 1), []).append((x, y))
    out = []
    while nxt:
        # The leftmost corner always has one outgoing step. Thus, the loop does not
        # start at a corner that two diagonal pixels share.
        start = a = min(nxt)
        d = None
        pts = []
        while True:
            outs = nxt[a]
            b = outs[0]
            if len(outs) > 1:
                # At a corner that two diagonal pixels share, turn right. Thus, diagonal
                # pixels are in separate outlines.
                right = (a[0] - d[1], a[1] + d[0])
                b = right if right in outs else outs[0]
            outs.remove(b)
            if not outs:
                del nxt[a]
            nd = (b[0] - a[0], b[1] - a[1])
            if nd != d:
                pts.append(a)
            d, a = nd, b
            if a == start:
                break
        if len(pts) > 2 and _collinear(pts[-1], pts[0], pts[1]):
            pts.pop(0)
        out.append(pts)
    return out


def _collinear(a, b, c):
    return (b[0] - a[0]) * (c[1] - b[1]) == (b[1] - a[1]) * (c[0] - b[0])


def area(pts):
    return sum(pts[i - 1][0] * p[1] - p[0] * pts[i - 1][1]
               for i, p in enumerate(pts)) / 2.0


def _dp(pts, tol):
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        ax, ay = pts[i]
        dx, dy = pts[j][0] - ax, pts[j][1] - ay
        ln = math.hypot(dx, dy) or 1.0
        far, best = 0.0, None
        for k in range(i + 1, j):
            dist = abs((pts[k][0] - ax) * dy - (pts[k][1] - ay) * dx) / ln
            if dist > far:
                far, best = dist, k
        if best is not None and far > tol:
            keep[best] = True
            stack += [(i, best), (best, j)]
    return [p for p, k in zip(pts, keep) if k]


def simplify(pts, tol):
    # Apply the Douglas-Peucker method to a closed outline.
    p0 = pts[0]
    far = max(range(len(pts)), key=lambda i: (pts[i][0] - p0[0]) ** 2
              + (pts[i][1] - p0[1]) ** 2)
    if far == 0:
        return list(pts)
    return (_dp(pts[:far + 1], tol)[:-1]
            + _dp(pts[far:] + [p0], tol)[:-1])


def _norm(k):
    return -_DIRS[k][1], _DIRS[k][0]


def _cross(e1, e2):
    # Get the corner where the lines of two edges meet. An edge is (dir, offset).
    # The line of the edge is normal(dir) . p == offset.
    n1, n2 = _norm(e1[0]), _norm(e2[0])
    det = n1[0] * n2[1] - n1[1] * n2[0]
    return ((e1[1] * n2[1] - n1[1] * e2[1]) / det,
            (n1[0] * e2[1] - e1[1] * n2[0]) / det)


def octilinear(pts, chamfer_leg):
    # Make a closed outline again from edges at multiples of 45 degrees, as a PCB
    # router does. Right-angle corners get a 45 degree chamfer. A base edge is
    # (dir, length, length * offset, start, end). The line of the edge is
    # normal(dir) . p == offset.
    base = []
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy)
        if ln == 0:
            continue
        k = int(round(math.atan2(dy, dx) / (math.pi / 4))) % 8
        n = _norm(k)
        base.append((k, ln, ln * (n[0] * (a[0] + b[0]) + n[1] * (a[1] + b[1])) / 2,
                     a, b))
    while True:
        # Adjacent base edges with the same direction become one edge.
        runs = []
        for i, e in enumerate(base):
            if runs and base[runs[-1][0]][0] == e[0]:
                runs[-1].append(i)
            else:
                runs.append([i])
        if len(runs) > 1 and base[runs[0][0]][0] == base[runs[-1][0]][0]:
            runs[0] = runs.pop() + runs[0]
        if len(runs) < 3:
            return None
        merged = [(base[r[0]][0], sum(base[i][1] for i in r),
                   sum(base[i][2] for i in r), base[r[0]][3], base[r[-1]][4])
                  for r in runs]
        # An edge is (dir, offset, index into merged).
        edges = []
        bad = None
        for i, m in enumerate(merged):
            prev = merged[i - 1]
            if (m[0] - prev[0]) % 8 == 4:
                # A U-turn needs a short edge between the two parallel lines.
                gap = -m[2] / m[1] - prev[2] / prev[1]
                if abs(gap) < 1e-9:
                    bad = min((i - 1) % len(merged), i, key=lambda q: merged[q][1])
                    break
                k = (prev[0] + (2 if gap > 0 else 6)) % 8
                n = _norm(k)
                mid = ((prev[4][0] + m[3][0]) / 2, (prev[4][1] + m[3][1]) / 2)
                edges.append((k, n[0] * mid[0] + n[1] * mid[1], -i - 1))
            edges.append((m[0], m[2] / m[1], i))
        if bad is None:
            corners = [_cross(edges[i - 1], e) for i, e in enumerate(edges)]
            for i, e in enumerate(edges):
                a, b = corners[i], corners[(i + 1) % len(edges)]
                d = _DIRS[e[0]]
                if (b[0] - a[0]) * d[0] + (b[1] - a[1]) * d[1] > 1e-9:
                    continue
                # The edge goes backwards. Remove it. For a U-turn connector, remove the
                # shorter of the two edges that it connects.
                j = e[2]
                if j < 0:
                    j = -j - 1
                    j = min((j - 1) % len(merged), j, key=lambda q: merged[q][1])
                if bad is None or merged[j][1] < merged[bad][1]:
                    bad = j
            if bad is None:
                break
        drop = set(runs[bad])
        base = [e for i, e in enumerate(base) if i not in drop]
    out = []
    n = len(corners)
    for i, c in enumerate(corners):
        k0, k1 = edges[i - 1][0], edges[i][0]
        if chamfer_leg > 0 and (k1 - k0) % 8 in (2, 6):
            p0, p1 = corners[i - 1], corners[(i + 1) % n]
            leg = min(chamfer_leg, 0.45 * math.hypot(c[0] - p0[0], c[1] - p0[1]),
                      0.45 * math.hypot(p1[0] - c[0], p1[1] - c[1]))
            d0, d1 = _DIRS[k0], _DIRS[k1]
            out.append((c[0] - d0[0] * leg, c[1] - d0[1] * leg))
            out.append((c[0] + d1[0] * leg, c[1] + d1[1] * leg))
        else:
            out.append(c)
    return out


def stepped(pts, tol, closed=True):
    # Follow an outline with runs at multiples of 45 degrees. Make each run as
    # long as possible in the tolerance (tol) of the outline. A curve becomes small
    # steps between two directions, not one long edge. An open line keeps its first
    # point and ends near its last point.
    dense = []
    for i, a in enumerate(pts if closed else pts[:-1]):
        b = pts[(i + 1) % len(pts)]
        steps = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / (tol / 2)))
        dense += [(a[0] + (b[0] - a[0]) * s / steps, a[1] + (b[1] - a[1]) * s / steps)
                  for s in range(steps)]
    if not closed:
        dense.append(pts[-1])
    n = len(dense)
    total = n if closed else n - 1
    q = dense[0]
    out = [] if closed else [q]
    done = 0
    while done < total:
        best = (0, 0, 0.0)
        for k, d in enumerate(_DIRS):
            run, far = 0, 0.0
            while done + run < total:
                c = dense[(done + run + 1) % n]
                rx, ry = c[0] - q[0], c[1] - q[1]
                along = rx * d[0] + ry * d[1]
                if abs(rx * d[1] - ry * d[0]) > tol or along < far - tol:
                    break
                far = max(far, along)
                run += 1
            if (run, far) > best[:1] + best[2:]:
                best = (run, k, far)
        if best[0] == 0:
            # No run fits from this point. Make a step onto the outline.
            done += 1
            q = dense[done % n]
            out.append(q)
            continue
        run, k, far = best
        q = (q[0] + _DIRS[k][0] * far, q[1] + _DIRS[k][1] * far)
        out.append(q)
        done += run
    return out


def centre_line(pts):
    # Get the middle line of a long thin closed shape. The result is an open line
    # from one tip to the other. The function calculates the average of the two
    # long sides.
    def far(p0):
        return max(range(len(pts)), key=lambda i: math.hypot(pts[i][0] - p0[0], pts[i][1] - p0[1]))

    a = far(pts[0])
    b = far(pts[a])
    if a > b:
        a, b = b, a
    side1 = pts[a:b + 1]
    side2 = (pts[b:] + pts[:a + 1])[::-1]

    def along(side, n):
        cum = [0.0]
        for q0, q1 in zip(side, side[1:]):
            cum.append(cum[-1] + math.hypot(q1[0] - q0[0], q1[1] - q0[1]))
        out = []
        for i in range(n + 1):
            t = cum[-1] * i / n
            j = min(bisect.bisect_right(cum, t) - 1, len(side) - 2)
            seg = cum[j + 1] - cum[j]
            f = (t - cum[j]) / seg if seg else 0.0
            out.append((side[j][0] + (side[j + 1][0] - side[j][0]) * f,
                        side[j][1] + (side[j + 1][1] - side[j][1]) * f))
        return out

    n = max(len(side1), len(side2), 8) * 2
    return [((q0[0] + q1[0]) / 2, (q0[1] + q1[1]) / 2)
            for q0, q1 in zip(along(side1, n), along(side2, n))]


def branch_thin(lines, traces, pitch):
    # Change thin lines into traces. If the end of a line is near a trace, the
    # line is a branch of that trace on the same net. A via is at its far tip, as
    # for a test point. A line that is not near a trace is a new net with a via at
    # each end. Return more traces. Each trace is (points, vias, net).
    def nearest(q):
        best = (float("inf"), q, None)
        for path, _, net in traces:
            for c, d in zip(path, path[1:]):
                dist, _, f = _closest(q, q, c, d)
                if dist < best[0]:
                    best = (dist, f, net)
        return best

    out = []
    next_net = 1 + max((t[2] for t in traces), default=-1)
    for line in lines:
        da, qa, na = nearest(line[0])
        db, qb, nb = nearest(line[-1])
        if da < db:
            line, dist, q, net = line[::-1], da, qa, na
        else:
            dist, q, net = db, qb, nb
        path = list(line)
        if dist <= 4 * pitch:
            # Make a 45 degree run and a straight run onto the trace.
            b = path[-1]
            dx, dy = q[0] - b[0], q[1] - b[1]
            m = min(abs(dx), abs(dy))
            path.append((b[0] + math.copysign(m, dx), b[1] + math.copysign(m, dy)))
            path.append(q)
            out.append((_dedupe(path), [path[0]], net))
        else:
            out.append((_dedupe(path), [path[0], path[-1]], next_net))
            next_net += 1
    return out


# Joining rings

class _Ring:
    def __init__(self, level, pts):
        self.level = level
        self.pts = pts
        self.cum = [0.0]
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            self.cum.append(self.cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = self.cum[-1]
        # These are the reserved lengths of the ring, as (centre, half length).
        self.busy = []
        # These are the slots that connect other rings here, as (start, end, slot
        # number, True if the path of the slot starts at `start`).
        self.kids = []

    def at(self, t):
        t %= self.length
        i = min(bisect.bisect_right(self.cum, t) - 1, len(self.pts) - 1)
        a, b = self.pts[i], self.pts[(i + 1) % len(self.pts)]
        seg = self.cum[i + 1] - self.cum[i]
        f = (t - self.cum[i]) / seg if seg else 0.0
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f

    def free(self, t, half):
        for u, h in self.busy:
            d = (t - u) % self.length
            if min(d, self.length - d) < half + h:
                return False
        return True

    def edges(self):
        n = len(self.pts)
        for i, a in enumerate(self.pts):
            b = self.pts[(i + 1) % n]
            ln = self.cum[i + 1] - self.cum[i]
            if ln > 0:
                yield i, a, b, ln


def _seg_grid(rings, cell):
    grid = {}
    for ri, r in enumerate(rings):
        for i, a, b, _ in r.edges():
            for gx in range(int(math.floor(min(a[0], b[0]) / cell)),
                            int(math.floor(max(a[0], b[0]) / cell)) + 1):
                for gy in range(int(math.floor(min(a[1], b[1]) / cell)),
                                int(math.floor(max(a[1], b[1]) / cell)) + 1):
                    grid.setdefault((gx, gy), []).append((ri, i))
    return grid


def _inside(pt, chains):
    # Use the even-odd test. Thus, holes are outside.
    x, y = pt
    inside = False
    for pts in chains:
        for i, (x1, y1) in enumerate(pts):
            x2, y2 = pts[(i + 1) % len(pts)]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _quad_hits(quad, a, b):
    # Return True if segment ab touches the convex quad.
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def inside(pt):
        s = [side(quad[i], quad[(i + 1) % 4], pt) for i in range(4)]
        return all(v >= 0 for v in s) or all(v <= 0 for v in s)

    if inside(a) or inside(b):
        return True
    for i in range(4):
        c, d = quad[i], quad[(i + 1) % 4]
        if side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0:
            return True
    return False


def _quads_overlap(a, b):
    return any(_quad_hits(a, b[i], b[(i + 1) % 4]) for i in range(4)) or \
        any(_quad_hits(b, a[i], a[(i + 1) % 4]) for i in range(4))


def bus(rings, pitch, track, via_d, nets=1, side=None, avoid=()):
    # Change closed rings into separate traces. Each trace has two via ends.
    # The rings are (level, points). Adjacent levels are one pitch apart.
    #
    # A port cuts a stack of rings where the rings are straight. This opens a
    # narrow slot. The slot becomes one pitch wider for each leg that goes through
    # it. The nets share the stack as bands of adjacent rings.
    #
    # A net goes around its first ring. At the cut, it steps across to the next
    # ring. Then it goes back around that ring, and continues in the same pattern.
    # Then its two ends go straight out through the slot. They fan out at 45 degrees
    # to a row of vias.
    #
    # Other rings connect through thin slots. From a ring that is already in a net,
    # the trace steps across a stack of nested rings. It goes around each ring in
    # sequence. Then it goes straight back through the middle of the slot.
    #
    # Some rings cannot connect, for example a pupil in an eye. Each of these rings
    # needs its own port and its own net. Thus, there can be more nets than the
    # requested number. If the stack is too shallow, there can be fewer nets.
    #
    # `side` is the unit direction for the vias (the y axis points down). If `side`
    # is None, the function selects the direction. Ports stay clear of the open
    # lines in `avoid`.
    # Return (traces, rings not used). Each trace is (points, vias, net).
    rings = [_Ring(lv, pts) for lv, pts in rings if len(pts) > 2]
    rings = [r for r in rings if r.length > 0]
    if not rings:
        return [], 0
    p = pitch
    gap = pitch - track
    vp = max(via_d + gap, p)
    outer = [r.pts for r in rings if r.level == 0]
    blocked = [(q0, q1) for pts in outer for q0, q1 in zip(pts, pts[1:] + pts[:1])]
    blocked += [seg for line in avoid for seg in zip(line, line[1:])]
    grid = _seg_grid(rings, 2 * p)

    def hit(q, level, u):
        # Find the straight ring edge at `level` through q, parallel to u. Return
        # (ring, arc position of q, space along -u, space along +u, True if the ring
        # goes along +u).
        gx, gy = int(math.floor(q[0] / (2 * p))), int(math.floor(q[1] / (2 * p)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ri, j in grid.get((gx + dx, gy + dy), ()):
                    r = rings[ri]
                    if r.level != level:
                        continue
                    a, b = r.pts[j], r.pts[(j + 1) % len(r.pts)]
                    ln = r.cum[j + 1] - r.cum[j]
                    v = ((b[0] - a[0]) / ln, (b[1] - a[1]) / ln)
                    if abs(u[0] * v[1] - u[1] * v[0]) > 1e-6:
                        continue
                    if abs((q[0] - a[0]) * v[1] - (q[1] - a[1]) * v[0]) > 0.03 * p:
                        continue
                    s = (q[0] - a[0]) * v[0] + (q[1] - a[1]) * v[1]
                    if not 0 <= s <= ln:
                        continue
                    if u[0] * v[0] + u[1] * v[1] > 0:
                        return ri, r.cum[j] + s, s, ln - s, True
                    return ri, r.cum[j] + s, ln - s, s, False
        return None

    def bands(m, count):
        # Divide the stack levels 0..m between the nets, as (first, last) levels.
        size = (m + 1) / count
        cuts = [int(round(i * size)) for i in range(count + 1)]
        return [(cuts[i], cuts[i + 1] - 1) for i in range(count)]

    def start_side(n):
        return -1 if n % 2 == 0 else 1

    def fan(m, groups, lead):
        # Find the destination of each net end, in port coordinates (u is along the
        # edge, v points out). The result is {(side, level): (x0, x1, a, b)}. The leg
        # goes straight out at u = x0 to v = a. Then it goes at 45 degrees to u = x1
        # at v = b. The legs go straight out for the distance `lead` before they fan
        # out. Also return the via positions and the length of the cut of each ring on
        # each side. The first nets start on alternate sides to balance the slot. A net
        # changes side at each ring. Deeper legs use the columns nearest the middle.
        ends = {-1: [], 1: []}
        for n, (first, last) in enumerate(groups):
            start = start_side(n)
            ends[start].append(first)
            ends[start if (last - first) % 2 else -start].append(last)
        legs = {}
        for sgn, levels in ends.items():
            levels.sort(reverse=True)
            x1 = None
            for rank, k in enumerate(levels):
                x0 = p / 2 + rank * p
                x1 = max(x0, vp / 2 + rank * vp, x1 + vp if x1 is not None else 0.0)
                a = lead + p + (len(levels) - 1 - rank) * p / 2
                legs[(sgn, k)] = (x0, x1, a, a + x1 - x0)
        end = max(l[3] for l in legs.values()) + max(p, via_d / 2)
        reach = {}
        for sgn, levels in ends.items():
            for k in range(m + 1):
                # This is a maximum of one pitch beyond the legs that go through this ring.
                reach[(sgn, k)] = legs[(sgn, k)][0] if (sgn, k) in legs else \
                    p / 2 + sum(1 for lv in levels if lv > k) * p
        return legs, end, reach

    def areas(c, u, nrm, m, groups, lead):
        # Get the space that a port needs outside the shape. The straight legs need
        # a narrow stem. The fan and the vias need a wide box.
        legs, end, _ = fan(m, groups, lead)

        def box(w, v0, v1):
            return [(c[0] + u[0] * uu + nrm[0] * vv, c[1] + u[1] * uu + nrm[1] * vv)
                    for uu, vv in ((-w, v0), (w, v0), (w, v1), (-w, v1))]
        wide = max(l[1] for l in legs.values())
        fan_box = box(wide + via_d / 2 + p, max(p / 2, lead), end + via_d / 2 + p)
        if lead == 0:
            return [fan_box]
        return [box(max(l[0] for l in legs.values()) + p, p / 2, lead + p), fan_box]

    # Find all possible port positions, as (spot, along, outwards, stack).
    cands = []
    for r0i, r0 in enumerate(rings):
        if r0.level:
            continue
        for i, a, b, ln in r0.edges():
            if ln < 1.2 * p:
                continue
            u = ((b[0] - a[0]) / ln, (b[1] - a[1]) / ln)
            nrm = (-u[1], u[0])
            mid = (a[0] + u[0] * ln / 2, a[1] + u[1] * ln / 2)
            if _inside((mid[0] + nrm[0] * p / 4, mid[1] + nrm[1] * p / 4), outer):
                nrm = (u[1], -u[0])
            # Short edges get a position only in the middle.
            o = p if ln >= 2 * p else ln / 2
            while o <= max(ln - p, ln / 2) + 1e-9:
                c = (a[0] + u[0] * o, a[1] + u[1] * o)
                stack = [(r0i, r0.cum[i] + o, o, ln - o, True)]
                while True:
                    k = len(stack)
                    h = hit((c[0] - nrm[0] * k * p, c[1] - nrm[1] * k * p), k, u)
                    if h is None:
                        break
                    stack.append(h)
                cands.append((c, u, nrm, stack))
                o += p / 2

    # Find all straight columns across nested rings where a slot can go. Each
    # column is a list of the rings along it, in the format that hit() gives.
    columns = []
    for ai, ra in enumerate(rings):
        for i, a, b, ln in ra.edges():
            if ln < 2 * p:
                continue
            u = ((b[0] - a[0]) / ln, (b[1] - a[1]) / ln)
            for nrm in ((-u[1], u[0]), (u[1], -u[0])):
                for step in (1, -1):
                    o = p / 2
                    while o <= ln - 1.5 * p + 1e-9:
                        c = (a[0] + u[0] * o, a[1] + u[1] * o)
                        chain = [(ai, ra.cum[i] + o, o, ln - o, True)]
                        while True:
                            k = len(chain)
                            h = hit((c[0] + nrm[0] * k * p, c[1] + nrm[1] * k * p),
                                    ra.level + step * k, u)
                            if h is None:
                                break
                            chain.append(h)
                        if len(chain) > 1:
                            columns.append(chain)
                        o += p

    # Rings that slots can connect need one port between them.
    parent = list(range(len(rings)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for chain in columns:
        for x, y in zip(chain, chain[1:]):
            parent[find(x[0])] = find(y[0])
    groups_of = {}
    for i in range(len(rings)):
        groups_of.setdefault(find(i), []).append(i)
    parts = sorted(groups_of.values(), key=lambda g: -sum(rings[i].length for i in g))

    # Do the small parts first. Then the largest part gets all the remaining nets.
    ports = []
    leads = [0.0] + [k * p for k in (2, 5, 10, 15, 20, 30, 40)]
    taken = set()
    placed = []
    for part in parts[:0:-1] + parts[:1]:
        members = set(part)
        wanted = max(1, nets - len(ports)) if part is parts[0] else 1
        # A small part without space for full corners (for example a pupil) can
        # open with a tighter cut.
        best = None
        for room in (p, p / 10):
            if best is not None:
                break
            options = []
            for c, u, nrm, stack in cands:
                if stack[0][0] not in members or stack[0][0] in taken:
                    continue
                depth = 0
                while depth < len(stack) and stack[depth][0] in members \
                        and min(stack[depth][2], stack[depth][3]) >= p / 2 + room:
                    depth += 1
                if depth == 0:
                    continue
                dot = nrm[1] if side is None else nrm[0] * side[0] + nrm[1] * side[1]
                along = c[1] if side is None else c[0] * side[0] + c[1] * side[1]
                aligned = side is not None and dot > 0.99
                options.append(((aligned, min(depth, wanted), depth, round(dot, 6), along),
                                c, u, nrm, stack[:depth]))
            options.sort(key=lambda o: o[0], reverse=True)
            for _, c, u, nrm, stack in options:
                # Find the deepest part of the stack where all the rings have space for the
                # cut and its corners.
                m = len(stack) - 1
                while m >= 0:
                    groups = bands(m, min(wanted, m + 1))
                    reach = fan(m, groups, 0.0)[2]
                    if all(min(st[2], st[3]) >= max(reach[(-1, k)], reach[(1, k)]) + room
                           for k, st in enumerate(stack[:m + 1])):
                        break
                    m -= 1
                if m < 0 or (best is not None and len(groups) <= len(best[5])):
                    continue
                for lead in leads:
                    quads = areas(c, u, nrm, m, groups, lead)
                    if any(_quad_hits(q, q0, q1) for q in quads for q0, q1 in blocked):
                        continue
                    if any(_quads_overlap(q, other) for q in quads for other in placed):
                        continue
                    best = (c, u, nrm, m, stack[:m + 1], groups, lead, quads)
                    break
                if best is not None and len(best[5]) >= wanted:
                    break
        if best is not None:
            c, u, nrm, m, stack, groups, lead, quads = best
            placed += quads
            ports.append((c, u, nrm, m, stack, groups, lead))
            reach = fan(m, groups, lead)[2]
            for k, st in enumerate(stack):
                rings[st[0]].busy.append((st[1], max(reach[(-1, k)], reach[(1, k)]) + p))
                taken.add(st[0])

    if not taken:
        return [], len(rings)

    def spot(st, uu):
        # Get the arc position on a ring of port or slot coordinate uu.
        return st[1] + uu if st[4] else st[1] - uu

    def span(i, last):
        # Get the length that a slot cuts from ring i of its column, in slot
        # coordinates. Ring 0 and the last ring keep the middle column for the return
        # track. The steps across change side at each ring.
        if i == 0:
            return 0.0, p
        if i == last:
            return (0.0, p) if i % 2 else (-p, 0.0)
        return -p, p

    def fits(st, lo, hi):
        return st[2] >= p / 2 - lo and st[3] >= p / 2 + hi and rings[st[0]].free(
            spot(st, (lo + hi) / 2), (hi - lo) / 2 + p / 2)

    # Do the longest stacks first. This decreases the number of slots.
    slots = []
    while True:
        best, best_key = None, None
        for chain in columns:
            if chain[0][0] not in taken:
                continue
            if not fits(chain[0], *span(0, 1)):
                continue
            last = 0
            for i in range(1, len(chain)):
                st = chain[i]
                if st[0] in taken:
                    break
                if fits(st, *span(i, i)):
                    last = i
                if not fits(st, -p, p):
                    break
            if last == 0:
                continue
            key = (last, min(min(st[2], st[3]) for st in chain[:last + 1]))
            if best_key is None or key > best_key:
                best, best_key = chain[:last + 1], key
        if best is None:
            break
        last = len(best) - 1
        for i, st in enumerate(best):
            lo, hi = span(i, last)
            rings[st[0]].busy.append((spot(st, (lo + hi) / 2), (hi - lo) / 2 + p / 2))
            taken.add(st[0])
        lo, hi = span(0, last)
        a0, a1 = sorted((spot(best[0], lo), spot(best[0], hi)))
        # The path of the slot starts at the end on the + side of the cut.
        rings[best[0][0]].kids.append((a0, a1, len(slots), a0 == spot(best[0], hi)))
        slots.append(best)

    def round_ring(ri, a0, a1):
        # Get the full ring from the end of its cut at a1 around to the end at a0.
        # Splice in the rings that connect to it.
        r = rings[ri]
        start, stop = a1, a0 + r.length
        events = [(ct + lap, 0, r.pts[i]) for i, ct in enumerate(r.cum[:-1])
                  for lap in (-r.length, 0, r.length, 2 * r.length) if start < ct + lap < stop]
        for k0, k1, slot, from_start in r.kids:
            e = start + (k0 - start) % r.length
            if e < stop:
                events.append((e, 1, (k1 - k0, slot, from_start)))
        events.sort(key=lambda ev: ev[0])
        out = [r.at(start)]
        for e, kind, data in events:
            if kind == 0:
                out.append(data)
                continue
            out.append(r.at(e))
            inner = slot_path(data[1])
            out += inner if data[2] else inner[::-1]
            out.append(r.at(e + data[0]))
        out.append(r.at(stop))
        return out

    def slot_path(slot):
        # Go around each ring of a slot in sequence. Each piece starts where the
        # step across from the previous ring stops.
        chain = slots[slot]
        last = len(chain) - 1
        out = []
        for i in range(1, last + 1):
            st = chain[i]
            lo, hi = span(i, last)
            a0, a1 = sorted((spot(st, lo), spot(st, hi)))
            piece = round_ring(st[0], a0, a1)
            # The trace goes into odd rings on side + and into even rings on side -.
            # round_ring starts at the higher arc position a1.
            out += piece if a1 == spot(st, p if i % 2 else -p) else piece[::-1]
        return out

    traces = []
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 3 * len(rings) + 100))
    try:
        for c, u, nrm, m, stack, groups, lead in ports:
            legs, end, reach = fan(m, groups, lead)

            def at(uu, vv):
                return (c[0] + u[0] * uu + nrm[0] * vv, c[1] + u[1] * uu + nrm[1] * vv)

            def leg(sgn, k):
                x0, x1, a, b = legs[(sgn, k)]
                return [at(sgn * x1, end), at(sgn * x1, b), at(sgn * x0, a)]

            for n, (first, last) in enumerate(groups):
                sgn = start_side(n)
                path = leg(sgn, first)
                for k in range(first, last + 1):
                    ri, t = stack[k][0], stack[k][1]
                    a0, a1 = sorted((spot(stack[k], reach[(1, k)]), spot(stack[k], -reach[(-1, k)])))
                    piece = round_ring(ri, a0, a1)
                    # The piece must start on the current side of the net.
                    q = piece[0]
                    if ((q[0] - c[0]) * u[0] + (q[1] - c[1]) * u[1] > 0) != (sgn > 0):
                        piece.reverse()
                    path += piece
                    sgn = -sgn
                path += leg(sgn, last)[::-1]
                vias = [path[0], path[-1]]
                traces.append((chamfer(_dedupe(path), p / 2), vias, len(traces)))
    finally:
        sys.setrecursionlimit(limit)
    return traces, len(rings) - len(taken)


def _hull(pts):
    # Get the convex hull. It is counter-clockwise when the y axis points up.
    pts = sorted(set(pts))

    def half(seq):
        out = []
        for q in seq:
            while len(out) > 1 and ((out[-1][0] - out[-2][0]) * (q[1] - out[-2][1])
                                    - (out[-1][1] - out[-2][1]) * (q[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(q)
        return out[:-1]
    return half(pts) + half(pts[::-1])


def octagon(c, r):
    # Make a regular octagon with edges on the 45 degree grid. r is the distance
    # from the centre to an edge.
    k = r / math.cos(math.pi / 8)
    return [(c[0] + k * math.cos(math.pi / 8 + j * math.pi / 4),
             c[1] + k * math.sin(math.pi / 8 + j * math.pi / 4)) for j in range(8)]


def swept(a, b, r):
    # Get the area that an octagon covers when it moves along segment ab. If ab
    # is on the 45 degree grid, all of the edges stay on the grid.
    return _hull(octagon(a, r) + octagon(b, r))


def pinches(chains, pitch):
    # Find the positions where closed outlines are closer than one pitch to
    # each other. Also find the positions where an outline is closer than one pitch
    # to a part of itself that is more than three pitches away along the outline.
    # Sharp corners that touch tip to tip cause these positions.
    segs, grid, cell = [], {}, pitch
    for ci, pts in enumerate(chains):
        run = 0.0
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            segs.append((ci, run, a, b))
            run += math.hypot(b[0] - a[0], b[1] - a[1])
            for gx in range(int(min(a[0], b[0]) // cell) - 1, int(max(a[0], b[0]) // cell) + 2):
                for gy in range(int(min(a[1], b[1]) // cell) - 1, int(max(a[1], b[1]) // cell) + 2):
                    grid.setdefault((gx, gy), []).append(len(segs) - 1)
    total = {}
    for ci, run, a, b in segs:
        total[ci] = run + math.hypot(b[0] - a[0], b[1] - a[1])
    seen, out = set(), []
    for ids in grid.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = min(ids[x], ids[y]), max(ids[x], ids[y])
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                ci, ri, a, b = segs[i]
                cj, rj, c, d = segs[j]
                dist, p, q = _closest(a, b, c, d)
                if dist >= 0.98 * pitch:
                    continue
                if ci == cj:
                    along = abs(ri + math.hypot(p[0] - a[0], p[1] - a[1])
                                - rj - math.hypot(q[0] - c[0], q[1] - c[1]))
                    if min(along, total[ci] - along) <= 3 * pitch:
                        continue
                m = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                if all(math.hypot(m[0] - o[0], m[1] - o[1]) > pitch for o in out):
                    out.append(m)
    return out


def _closest(a, b, c, d):
    # Get the distance between segments ab and cd, and the two closest points.
    def foot(p, s, e):
        dx, dy = e[0] - s[0], e[1] - s[1]
        ln = dx * dx + dy * dy
        f = max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / ln)) if ln else 0.0
        return s[0] + dx * f, s[1] + dy * f
    best = None
    for p, s, e, first in ((a, c, d, True), (b, c, d, True),
                           (c, a, b, False), (d, a, b, False)):
        f = foot(p, s, e)
        dist = math.hypot(f[0] - p[0], f[1] - p[1])
        if best is None or dist < best[0]:
            best = (dist, p, f) if first else (dist, f, p)
    return best


def min_gap(path, apart):
    # Get the smallest distance between two pieces of the path that are more
    # than `apart` from each other along the path. The self-check uses this function.
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    cell = apart
    grid = {}
    for i, (a, b) in enumerate(zip(path, path[1:])):
        for gx in range(int(min(a[0], b[0]) // cell) - 1, int(max(a[0], b[0]) // cell) + 2):
            for gy in range(int(min(a[1], b[1]) // cell) - 1, int(max(a[1], b[1]) // cell) + 2):
                grid.setdefault((gx, gy), []).append(i)
    best = float("inf")
    for segs in grid.values():
        for x in range(len(segs)):
            for y in range(x + 1, len(segs)):
                i, j = sorted((segs[x], segs[y]))
                if cum[j] - cum[i + 1] <= apart:
                    continue
                best = min(best, _seg_dist(path[i], path[i + 1], path[j], path[j + 1]))
    return best


def _seg_dist(a, b, c, d):
    def pt(p, s, e):
        dx, dy = e[0] - s[0], e[1] - s[1]
        ln = dx * dx + dy * dy
        f = max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / ln)) if ln else 0.0
        return math.hypot(s[0] + dx * f - p[0], s[1] + dy * f - p[1])
    return min(pt(a, c, d), pt(b, c, d), pt(c, a, b), pt(d, a, b))


if pcbnew:

    def _poly(loop_list):
        poly = pcbnew.SHAPE_POLY_SET()
        for pts in loop_list:
            chain = pcbnew.SHAPE_LINE_CHAIN()
            for x, y in pts:
                chain.Append(int(round(x)), int(round(y)))
            chain.SetClosed(True)
            poly.AddOutline(chain)
        return poly

    def _op(poly, name, *args):
        # KiCad 8 needs an additional polygon mode argument. KiCad 9 and later do not.
        try:
            getattr(poly, name)(*args)
        except TypeError:
            getattr(poly, name)(*(args + (pcbnew.SHAPE_POLY_SET.PM_FAST,)))

    def _nested(outlines):
        # Make a region from outlines that have holes and shapes in those holes
        # (for example a pupil in an eye). Start with the largest outline and go in.
        # Each outline adds to or cuts from the current region.
        region = pcbnew.SHAPE_POLY_SET()
        for p in sorted(outlines, key=lambda q: -abs(area(q))):
            _op(region, "BooleanAdd" if area(p) > 0 else "BooleanSubtract", _poly([p]))
        return region

    def _clean(region, pitch, err):
        # Remove the parts of a ring region where the boundary is closer than one
        # pitch to itself. These are necks narrower than one pitch in the region, and
        # the two sides of gaps narrower than one pitch outside the region.
        miter = pcbnew.CORNER_STRATEGY_ALLOW_ACUTE_CORNERS
        r = pitch // 2

        def opened(p):
            o = pcbnew.SHAPE_POLY_SET(p)
            o.Deflate(r, miter, err)
            o.Inflate(r, miter, err)
            _op(o, "BooleanIntersection", p)
            return o

        region = opened(region)
        gaps = pcbnew.SHAPE_POLY_SET(region)
        gaps.Inflate(r, miter, err)
        gaps.Deflate(r, miter, err)
        _op(gaps, "BooleanSubtract", region)
        # The rounding leaves hairlines along the edges. These hairlines are not gaps.
        gaps.Deflate(2 * err, miter, err)
        if gaps.OutlineCount():
            # Grow them by one pitch with an octagon. The octagon stays compact and
            # on the 45 degree grid.
            grown = pcbnew.SHAPE_POLY_SET()
            for c in _chains(gaps):
                _op(grown, "BooleanAdd", _poly(swept(a, b, pitch + 2 * err)
                                               for a, b in zip(c, c[1:] + c[:1])))
            _op(region, "BooleanSubtract", grown)
            region = opened(region)
        # Sharp corners that touch tip to tip stay after the two steps above. At each
        # of these positions, cut an octagon with a radius of one pitch out of the region.
        for _ in range(5):
            spots = pinches(_chains(region), pitch)
            if not spots:
                break
            _op(region, "BooleanSubtract", _poly(octagon(q, pitch) for q in spots))
            region = opened(region)
        return region

    def _chains(region):
        out = []
        for i in range(region.OutlineCount()):
            for c in [region.Outline(i)] + [region.Hole(i, h)
                                            for h in range(region.HoleCount(i))]:
                out.append([(c.CPoint(j).x, c.CPoint(j).y)
                            for j in range(c.PointCount())])
        return out

    def rings(outlines, width, pitch, levels=0):
        # Get the centre lines of concentric tracks that fill the outlines, as
        # (level, points). The tracks are one pitch apart. The maximum depth is `levels`
        # (0 fills the full shape). The values are in board units (nm). Each level is the
        # previous level shrunk by one pitch. Thus, adjacent levels are never closer
        # than one pitch.
        region = _nested(outlines)
        miter = pcbnew.CORNER_STRATEGY_ALLOW_ACUTE_CORNERS
        err = max(1000, width // 50)
        out = []
        region.Deflate(width // 2, miter, err)
        level = 0
        while True:
            region = _clean(region, pitch, err)
            if region.OutlineCount() == 0 or levels and level >= levels:
                return out
            out += [(level, c) for c in _chains(region)]
            region = pcbnew.SHAPE_POLY_SET(region)
            region.Deflate(pitch, miter, err)
            level += 1

    def outline_rings(on, w, h, length, width, track, gap, levels=0, angle45=True):
        # Get the rings in mm for a w x h mask that has a size of length x width mm.
        # The origin is at the top-left corner of the mask. If angle45 is not set, the
        # rings follow the outline at all angles.
        pitch = track + gap
        nm = pcbnew.FromMM(1)
        sx, sy = length / w * nm, width / h * nm
        raw = [[(x * sx, y * sy) for x, y in p] for p in loops(on, w, h)]
        region = _nested(raw)
        # Parts and slits narrower than two pitches cannot hold tracks. They only
        # leave spikes. Thus, remove the whiskers and thin gaps first.
        smooth = pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS
        r = pcbnew.FromMM(pitch)
        err = max(1000, r // 50)
        region.Deflate(r, smooth, err)
        region.Inflate(r, smooth, err)
        region.Inflate(r, smooth, err)
        region.Deflate(r, smooth, err)
        outlines = []
        for i in range(region.OutlineCount()):
            for c, sign in [(region.Outline(i), 1)] + [(region.Hole(i, h), -1)
                                                       for h in range(region.HoleCount(i))]:
                p = [(c.CPoint(j).x / nm, c.CPoint(j).y / nm)
                     for j in range(c.PointCount())]
                if angle45:
                    q = octilinear(stepped(p, TRACE_TOL * pitch), pitch)
                else:
                    q = simplify(p, TRACE_TOL * pitch)
                if q is None or len(q) < 3:
                    continue
                if area(q) * sign < 0:
                    q.reverse()
                outlines.append([(pcbnew.FromMM(x), pcbnew.FromMM(y)) for x, y in q])
        if not any(area(p) > 0 for p in outlines):
            return []
        found = rings(outlines, pcbnew.FromMM(track), pcbnew.FromMM(pitch), levels)
        return [(lv, [(x / nm, y / nm) for x, y in pts]) for lv, pts in found]

    def thin_lines(on, w, h, length, width, track, gap):
        # Get the middle lines in mm of the parts that are too thin for two tracks,
        # for example whiskers. outline_rings removes these parts when it cleans the outline.
        pitch = track + gap
        nm = pcbnew.FromMM(1)
        sx, sy = length / w * nm, width / h * nm
        full = _nested([[(x * sx, y * sy) for x, y in p] for p in loops(on, w, h)])
        smooth = pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS
        r = pcbnew.FromMM(pitch)
        err = max(1000, r // 50)
        thin = pcbnew.SHAPE_POLY_SET(full)
        full.Deflate(r, smooth, err)
        full.Inflate(r, smooth, err)
        _op(thin, "BooleanSubtract", full)
        # The pixel steps along the edges also go into this set. They are thinner
        # than all real lines. Thus, a small opening removes them.
        eps = int(max(sx, sy) * 0.6)
        thin.Deflate(eps, smooth, err)
        thin.Inflate(eps, smooth, err)
        out = []
        for c in _chains(thin):
            if len(c) < 4:
                continue
            line = centre_line([(x / nm, y / nm) for x, y in c])
            run = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(line, line[1:]))
            if run >= 3 * pitch:
                out.append(stepped(line, TRACE_TOL * pitch, closed=False))
        return out


if __name__ == "__main__":
    shapes = ({(0, 0)}, {(0, 0), (1, 0)},
              {(x, y) for x in range(5) for y in range(4)} - {(2, 1)},
              {(0, 0), (1, 0), (1, 1), (5, 5)})
    for cells in shapes:
        p = rows(cells)
        half_cells = {(2 * x + dx, 2 * y + dy) for x, y in _component(cells)
                      for dx in (0, 1) for dy in (0, 1)}
        assert len(p) == len(half_cells) and set(p) == half_cells, (cells, p)
        assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
                   for a, b in zip(p, p[1:]))
        assert abs(p[0][0] - p[-1][0]) + abs(p[0][1] - p[-1][1]) == 1
    assert rows(set()) == []
    assert chamfer(rows({(0, 0), (1, 0)}), 0.5) == [
        (0, 0), (0, 0.5), (0.5, 1), (2.5, 1), (3, 0.5), (2.5, 0), (1, 0)]
    stair = [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2), (3, 2)]
    assert chamfer(stair, 0.5) == [(0, 0), (0.5, 0), (1, 0.5), (1.5, 1),
                                   (2, 1.5), (2.5, 2), (3, 2)]

    sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert loops(bytearray([1]), 1, 1) == [sq]
    ring = loops(bytearray([1, 1, 1, 1, 0, 1, 1, 1, 1]), 3, 3)
    assert sorted(area(p) for p in ring) == [-1.0, 9.0]
    assert len(loops(bytearray([1, 0, 0, 1]), 2, 2)) == 2
    assert simplify([(0, 0), (1, 0.01), (2, 0), (2, 2), (0, 2)], 0.1) == \
        [(0, 0), (2, 0), (2, 2), (0, 2)]
    oc = octilinear([(0, 0), (10, 0), (10, 10), (0, 10)], 1)
    assert len(oc) == 8 and abs(area(oc) - 98) < 1e-6
    circle = [(40 * math.cos(a * math.pi / 25), 40 * math.sin(a * math.pi / 25))
              for a in range(50)]
    assert len(octilinear(circle, 0)) == 8

    wave = [(x * 0.5, 0.3 * math.sin(x / 3.0)) for x in range(40)]
    st = stepped(wave, 0.2, closed=False)
    assert st[0] == wave[0] and math.hypot(st[-1][0] - wave[-1][0], st[-1][1] - wave[-1][1]) < 0.5
    for a, b in zip(st, st[1:]):
        d = (abs(b[0] - a[0]), abs(b[1] - a[1]))
        assert d[0] < 1e-9 or d[1] < 1e-9 or abs(d[0] - d[1]) < 1e-9
    bar = [(0, 0), (5, 0), (10, 0), (10, 1), (5, 1), (0, 1)]
    mid = centre_line(bar)
    assert all(abs(q[1] - 0.5) < 0.51 for q in mid)
    assert abs(mid[len(mid) // 2][1] - 0.5) < 1e-6
    box = [(20, -5), (20, 5)]
    near_line = [(0, 0), (10, 0), (18, 0)]
    far_line = [(0, 50), (10, 50)]
    more = branch_thin([near_line, far_line], [(box, [], 7)], 1.0)
    assert more[0][0][-1] == (20, 0) and more[0][1] == [(0, 0)] and more[0][2] == 7
    assert more[1][1] == [(0, 50), (10, 50)] and more[1][2] == 8

    # Nested squares one unit apart. For all numbers of nets, each net has two
    # ends, all runs stay one unit apart and all the rings are used.
    nested = [(k, [(k, k), (30 - k, k), (30 - k, 30 - k), (k, 30 - k)])
              for k in range(6)]
    full = sum(4 * (30 - 2 * k) for k in range(6))
    for count in (1, 2, 3, 6):
        ts, left = bus(nested, 1.0, 0.5, 0.8, count)
        assert left == 0 and len(ts) == count and {t[2] for t in ts} == set(range(count))
        run = 0.0
        for path, vias, _ in ts:
            assert vias == [path[0], path[-1]]
            assert min_gap(path, 3.0) >= 1.0 - 1e-6
            run += sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                assert min(_seg_dist(p0, p1, q0, q1)
                           for p0, p1 in zip(a[0], a[0][1:])
                           for q0, q1 in zip(b[0], b[0][1:])) >= 1.0 - 1e-6
        assert run > 0.8 * full, (count, run, full)
    # Two pockets of nested rings. A port can cross only one of them. Thus, the
    # other pocket connects through a slot across all its rings. A run must not go
    # off the 45 degree grid. A slot end connected in the incorrect direction causes
    # this problem.
    pockets = [(0, [(0, 0), (61, 0), (61, 30), (0, 30)])] + [
        (k, [(x0 + k, k), (x1 - k, k), (x1 - k, 30 - k), (x0 + k, 30 - k)])
        for k in (1, 2, 3, 4) for x0, x1 in ((0, 31), (30, 61))]
    ts, left = bus(pockets, 1.0, 0.5, 0.8, 1)
    assert left == 0 and len(ts) == 1
    path = ts[0][0]
    assert min_gap(path, 3.0) >= 1.0 - 1e-6
    for a, b in zip(path, path[1:]):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        assert min(dx, dy, abs(dx - dy)) < 1e-6, (a, b)
    print("self-check ok")
