"""Prepared Breadcast graphics; only Program owns active cues and official score state."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
from pathlib import Path
import time
import uuid

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent / 'web'
PAPER, INK, GREEN, GOLD, MUTED = '#F7F4EC', '#292D25', '#45583F', '#EFA845', '#818773'

# Neutral copy is usable immediately. Supplied event facts can replace these bindings later.
SPECS = (
    ('opening', 'Fresh opening', 'Screens', 'screen', 'Welcome to breadcast.', 'Fresh perspectives. Served live.', 'rise'),
    ('countdown', 'Countdown', 'Screens', 'screen', 'Almost out of the oven.', 'Your broadcast starts soon.', 'countdown'),
    ('break', 'Back in a crumb', 'Screens', 'screen', 'Back in a crumb.', 'A little break. A lot more to come.', 'float'),
    ('closing', 'That’s a wrap', 'Screens', 'screen', 'That’s a wrap.', 'Thanks for sharing the view.', 'confetti'),
    ('spotlight', 'Spotlight card', 'Screens', 'screen', 'A fresh perspective.', 'Take a closer look.', 'spotlight'),
    ('matchup', 'Matchup card', 'Screens', 'screen', 'Two sides. One stage.', 'Ready for the next chapter.', 'split'),
    ('lower-classic', 'Classic name bar', 'Name bars', 'lower', 'Now on Breadcast', 'Good views. Freshly served.', 'slide'),
    ('lower-pill', 'Rounded name bar', 'Name bars', 'lower', 'A fresh perspective', 'Stay in the mix.', 'pop'),
    ('lower-split', 'Split name bar', 'Name bars', 'lower', 'In the spotlight', 'A closer look.', 'split'),
    ('lower-portrait', 'Toast name bar', 'Name bars', 'lower', 'The freshest view', 'Made to be shared.', 'rise'),
    ('caption', 'Caption card', 'Name bars', 'lower', 'Good things deserve a closer look.', '', 'fade'),
    ('ticker', 'Rolling ticker', 'Banners', 'ticker', 'Good bread. Great broadcasts.  •  Fresh perspectives, served live.', '', 'scroll'),
    ('headline', 'Headline ribbon', 'Banners', 'banner', 'A fresh take.', 'You’re watching Breadcast.', 'slide'),
    ('toast-note', 'Toast notification', 'Banners', 'banner', 'A little something fresh.', 'Stay for the good stuff.', 'pop'),
    ('wide-banner', 'Full-width banner', 'Banners', 'banner', 'Made to be shared.', 'Good views bring people together.', 'rise'),
    ('brand-bug', 'Breadcast corner logo', 'Corner marks', 'bug', 'breadcast.', '', 'fade'),
    ('status-bug', 'Program status badge', 'Corner marks', 'bug', 'breadcast.', '', 'pop'),
    ('corner-label', 'Corner label', 'Corner marks', 'bug', 'Fresh perspectives', '', 'slide'),
    ('score-compact', 'Compact scoreboard', 'Scoreboards', 'score', 'Scoreboard', '', 'flip'),
    ('score-wide', 'Wide scoreboard', 'Scoreboards', 'score', 'Scoreboard', '', 'split'),
    ('toast-wipe', 'Toast wipe', 'Stingers', 'stinger', 'breadcast.', '', 'wipe'),
    ('ribbon-sweep', 'Ribbon sweep', 'Stingers', 'stinger', 'Freshly served.', '', 'ribbons'),
    ('crumb-burst', 'Crumb confetti', 'Stingers', 'stinger', 'Good views.', '', 'confetti'),
    ('iris-reveal', 'Iris reveal', 'Stingers', 'stinger', 'breadcast.', '', 'iris'),
)
CATALOG = {row[0]: dict(zip(('id', 'name', 'category', 'slot', 'title', 'subtitle', 'motion'), row)) for row in SPECS}


def text(value, name, limit=120):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f'{name} must be plain text with at most {limit} characters')
    return value.strip()


def seconds(value, name, low=0, high=3600):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be {low}–{high} seconds')
    return float(value)


def score_values(data, confirmed=True):
    if not isinstance(data, dict) or set(data) - {'home', 'away', 'home_score', 'away_score', 'period', 'clock_seconds', 'clock_running', 'confirmed'}:
        raise ValueError('Unknown scoreboard field')
    if confirmed and data.get('confirmed') is not True:
        raise ValueError('Confirm that the score and clock values are official')
    result = {'home': text(data.get('home', ''), 'First team', 24),
              'away': text(data.get('away', ''), 'Second team', 24),
              'period': text(data.get('period', ''), 'Period', 16)}
    for key in ('home_score', 'away_score', 'clock_seconds'):
        value = data.get(key)
        high = 359999 if key == 'clock_seconds' else 999
        if value is not None and (type(value) is not int or not 0 <= value <= high):
            raise ValueError(f'{key} must be unknown or an integer from 0 to {high}')
        result[key] = value
    running = data.get('clock_running', False)
    if type(running) is not bool or (running and result['clock_seconds'] is None):
        raise ValueError('A running clock needs a supplied starting time')
    result['clock_running'] = running
    result['authority'] = 'operator-confirmed' if confirmed else 'preview only'
    return result


def ease(value):
    value = max(0., min(1., value))
    return 1 - (1 - value) ** 3


def png(image):
    buffer = io.BytesIO(); image.save(buffer, format='PNG'); return buffer.getvalue()


@dataclass(frozen=True)
class Prepared:
    op: str
    spec: dict | None = None
    title: str = ''
    subtitle: str = ''
    duration: float = 0
    layer: Image.Image | None = None
    score: dict | None = None
    slot: str | None = None
    bound_layers: dict | None = None
    keep_clock: bool = False


@dataclass(frozen=True)
class Cue:
    id: str
    prepared: Prepared
    started: float

    def summary(self):
        p = self.prepared
        return {'cue_id': self.id, 'preset': p.spec['id'], 'name': p.spec['name'], 'slot': p.spec['slot'],
                'title': p.title, 'subtitle': p.subtitle, 'duration_s': p.duration}


class Graphics:
    def __init__(self, width=640, height=360):
        if (width, height) != (640, 360):
            raise ValueError('This graphics package requires a 640×360 program')
        self.w, self.h = width, height
        self.fonts = {}
        for family in ('outfit', 'dmsans'):
            for size in (10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 42, 54, 64, 76):
                font = ImageFont.truetype(str(ROOT / 'fonts' / f'{family}.ttf'), size)
                axes = font.get_variation_axes()
                font.set_variation_by_axes([700 if axis['name'] == b'Weight' and family == 'outfit' else axis['default'] for axis in axes])
                self.fonts[family, size] = font
        with Image.open(ROOT / 'brand' / 'breadcast-mark.png') as image:
            self.logo = image.convert('RGBA')
        self.logo_sizes = {s: self.logo.resize((s, s), Image.Resampling.LANCZOS) for s in (30, 46, 64, 90)}
        self.active: dict[str, Cue] = {}
        self.retiring: dict[str, tuple[Cue, float]] = {}
        self.score = score_values({}, confirmed=False)
        self.score['authority'] = None
        self.score_anchor = time.monotonic()
        self.previous_score = self.score.copy()
        self.score_changed = 0.
        self.thumbnails = {}
        self.base_layers = {}
        self.event_package = None
        self.official_eligible = True
        for spec in CATALOG.values():
            layer = self.layer(spec, spec['title'], spec['subtitle'], self.score)
            self.base_layers[spec['id']] = layer
            prepared = Prepared('cue', spec, spec['title'], spec['subtitle'], 30 if spec['id'] == 'countdown' else 0, layer)
            cue = Cue('preview', prepared, 0)
            frame = Image.new('RGBA', (self.w, self.h), PAPER if spec['slot'] in ('screen', 'stinger') else '#C4CDB9')
            self.paint(frame, cue, 1.2, 'LIVE', self.score, preview=True)
            self.thumbnails[spec['id']] = png(frame.convert('RGB'))

    def font(self, size=20, family='dmsans'):
        return self.fonts[family, size]

    def write(self, image, pos, value, size=24, fill=INK, width=None, anchor=None, family='outfit'):
        draw = ImageDraw.Draw(image)
        for candidate in sorted({s for f, s in self.fonts if f == family and s <= size}, reverse=True):
            font = self.font(candidate, family)
            if width is None or draw.textlength(value, font=font) <= width:
                break
        if width and draw.textlength(value, font=font) > width:
            while value and draw.textlength(value + '…', font=font) > width:
                value = value[:-1]
            value += '…'
        draw.text(pos, value, font=font, fill=fill, anchor=anchor)

    def layer(self, spec, title, subtitle, score):
        slot, key = spec['slot'], spec['id']
        image = Image.new('RGBA', (self.w, self.h))
        draw = ImageDraw.Draw(image)
        if slot in ('screen', 'stinger'):
            dark = key in ('spotlight', 'iris-reveal', 'ribbon-sweep')
            image.paste(GREEN if dark else PAPER, (0, 0, self.w, self.h))
            draw = ImageDraw.Draw(image)
            draw.ellipse((-120, -130, 200, 190), fill='#4F6248' if dark else '#EDE6D6')
            draw.ellipse((480, 230, 760, 530), fill='#4F6248' if dark else '#EDE6D6')
            if key == 'matchup':
                draw.polygon([(0, 0), (340, 0), (290, self.h), (0, self.h)], fill='#EAE9D8')
                self.write(image, (150, 175), score['home'] or '—', 36, width=240, anchor='mm')
                self.write(image, (490, 175), score['away'] or '—', 36, width=240, anchor='mm')
                self.write(image, (320, 174), 'VS', 18, MUTED, anchor='mm')
                self.write(image, (320, 268), title, 28, width=550, anchor='mm')
            else:
                self.write(image, (320, 204), title, 36 if key != 'countdown' else 28,
                           PAPER if dark else INK, width=560, anchor='mm')
                self.write(image, (320, 252 if key != 'countdown' else 318), subtitle, 16,
                           '#D4DBC8' if dark else MUTED, width=530, anchor='mm', family='dmsans')
        elif slot == 'lower':
            left, top, right, bottom = (28, 252, 490, 328)
            fill = PAPER if key == 'lower-pill' else GREEN
            if key == 'caption':
                left, top, right, bottom = 55, 270, 585, 326
                fill = '#292D25'
            draw.rounded_rectangle((left, top, right, bottom), radius=34 if key == 'lower-pill' else 10, fill=fill)
            draw.rounded_rectangle((left, top, left + 9, bottom), radius=4, fill=GOLD)
            if key == 'lower-split':
                draw.rectangle((left + 9, top, right, top + 36), fill=PAPER)
            inset = 80 if key == 'lower-portrait' else 24
            if key == 'lower-portrait': image.alpha_composite(self.logo_sizes[46], (left + 13, top + 14))
            if key == 'caption':
                self.write(image, (320, 298), title, 20, PAPER, width=485, anchor='mm')
            else:
                self.write(image, (left + inset, top + 10), title, 24,
                           INK if key in ('lower-pill', 'lower-split') else PAPER, width=right-left-inset-20)
                self.write(image, (left + inset, top + 44), subtitle, 12,
                           MUTED if key == 'lower-pill' else '#D6DECA', width=right-left-inset-20, family='dmsans')
        elif slot == 'ticker':
            draw.rectangle((0, 330, self.w, 360), fill=GREEN)
            draw.rectangle((0, 330, 100, 360), fill=GOLD)
            self.write(image, (14, 337), 'breadcast.', 16, INK)
        elif slot == 'banner':
            if key == 'toast-note':
                draw.rounded_rectangle((330, 152, 615, 229), 12, fill=PAPER)
                image.alpha_composite(self.logo_sizes[46], (338, 166))
                self.write(image, (395, 167), title, 18, width=208)
                self.write(image, (395, 196), subtitle, 10, MUTED, width=208, family='dmsans')
            else:
                y = 156
                draw.rectangle((24, y, 616, y+58), fill=GOLD if key == 'headline' else GREEN)
                self.write(image, (46, y+8), title, 24, INK if key == 'headline' else PAPER, width=550)
                self.write(image, (46, y+38), subtitle, 10, INK if key == 'headline' else '#D6DECA', width=550, family='dmsans')
        elif slot == 'bug':
            draw.rounded_rectangle((448, 16, 616, 79 if key == 'status-bug' else 61), 10, fill=PAPER)
            image.alpha_composite(self.logo_sizes[30], (454, 23))
            self.write(image, (489, 24 if key == 'status-bug' else 30), title, 18 if key != 'corner-label' else 12, width=117)
        elif slot == 'score':
            wide = key == 'score-wide'
            x, y, width = (40, 66, 560) if wide else (28, 65, 430)
            draw.rounded_rectangle((x, y, x+width, y+66), 8, fill=INK)
            draw.rectangle((x, y, x+7, y+66), fill=GOLD)
            self.write(image, (x+20, y+10), score['home'] or '—', 16, PAPER, width=125 if wide else 80)
            self.write(image, (x+width//2+10, y+10), score['away'] or '—', 16, PAPER, width=125 if wide else 80)
            self.write(image, (x+20, y+38), score['period'] or '', 10, '#C3CEB6', width=130, family='dmsans')
            for key_, sx in [('home_score', x+width//2-50), ('away_score', x+width-50)]:
                self.write(image, (sx, y+13), '—' if score[key_] is None else str(score[key_]), 28, GOLD, width=44)
        return image

    def prepare(self, data, preview=False):
        if not isinstance(data, dict): raise ValueError('Expected a graphics command')
        op = data.get('op')
        fields = {'cue': {'op', 'preset', 'title', 'subtitle', 'duration_s', 'score'},
                  'score': {'op', 'score'}, 'clear': {'op', 'slot'}, 'clear-all': {'op'}}
        if op not in fields or set(data) - fields[op]: raise ValueError('Unknown graphics command or field')
        if op == 'score':
            values = data.get('score')
            score = score_values(values, confirmed=not preview)
            keep_clock = not ({'clock_seconds', 'clock_running'} & values.keys())
            if keep_clock:
                score.update({key: self.score[key] for key in ('clock_seconds', 'clock_running')})
            layers = {key: self.layer(CATALOG[key], '', '', score) for key in ('score-compact', 'score-wide')}
            screen = self.active.get('screen')
            if screen and screen.prepared.spec['id'] == 'matchup':
                p = screen.prepared
                layers['matchup'] = self.layer(p.spec, p.title, p.subtitle, score)
            return Prepared(op, score=score, bound_layers=layers, keep_clock=keep_clock)
        if op in ('clear', 'clear-all'):
            slot = data.get('slot')
            if op == 'clear' and slot not in {row[3] for row in SPECS}: raise ValueError('Unknown graphics layer')
            return Prepared(op, slot=slot)
        if op != 'cue' or data.get('preset') not in CATALOG:
            raise ValueError('Choose a graphics preset')
        spec = CATALOG[data['preset']]
        title = text(data.get('title', ''), 'Headline') or spec['title']
        subtitle = text(data.get('subtitle', spec['subtitle']), 'Subtitle', 160)
        duration = seconds(data.get('duration_s', 0), 'Duration')
        if spec['slot'] == 'stinger': duration = 2.
        if spec['id'] == 'countdown' and not 1 <= duration <= 3600:
            raise ValueError('Set a countdown duration from 1 to 3600 seconds')
        if 'score' in data and not preview:
            raise ValueError('Update confirmed score values before showing a scoreboard')
        score = self.score.copy() if data.get('score') is None else score_values(data['score'], confirmed=False)
        return Prepared(op, spec, title, subtitle, duration, self.layer(spec, title, subtitle, score), score)

    def apply(self, prepared, now):
        # Called only while Program holds its state lock.
        if prepared.op == 'score':
            self.previous_score = self.score.copy()
            self.score = prepared.score.copy(); self.score_changed = now
            if not prepared.keep_clock: self.score_anchor = now
            def update(old):
                p = old.prepared
                return Cue(old.id, Prepared(p.op, p.spec, p.title, p.subtitle, p.duration,
                                           prepared.bound_layers[p.spec['id']], self.score.copy()), old.started)
            if 'score' in self.active: self.active['score'] = update(self.active['score'])
            if 'score' in self.retiring:
                old, ended = self.retiring['score']; self.retiring['score'] = (update(old), ended)
            if 'matchup' in prepared.bound_layers and 'screen' in self.active:
                old = self.active['screen']; p = old.prepared
                self.active['screen'] = Cue(old.id, Prepared(p.op, p.spec, p.title, p.subtitle, p.duration,
                    prepared.bound_layers['matchup'], self.score.copy()), old.started)
            return
        if prepared.op == 'clear-all':
            for slot in list(self.active): self.clear(slot, now)
        elif prepared.op == 'clear':
            self.clear(prepared.slot, now)
        else:
            slot = prepared.spec['slot']
            self.retiring.pop(slot, None)
            self.active[slot] = Cue(uuid.uuid4().hex, prepared, now)

    def clear(self, slot, now):
        if slot in self.active:
            self.retiring[slot] = (self.active.pop(slot), now)

    def clear_cover(self):
        # Return-to-live and replay must reveal their requested source immediately.
        for slot in ('screen', 'stinger'):
            self.active.pop(slot, None); self.retiring.pop(slot, None)

    def public_state(self):
        return {'package': 'breadcast-default-v1', 'requested': [cue.summary() for cue in self.active.values()],
                'score': {**self.score, 'clock_kind': 'operator-controlled display clock'}}

    def clock(self, score, now):
        value = score['clock_seconds']
        if value is None: return '--:--'
        if score['clock_running']: value += max(0, int(now - self.score_anchor))
        value = min(value, 359999)
        return f'{value//3600:02d}:{value//60%60:02d}:{value%60:02d}' if value >= 3600 else f'{value//60:02d}:{value%60:02d}'

    def paint(self, frame, cue, now, mode, score, preview=False, exit_alpha=1.):
        p, age = cue.prepared, max(0., now-cue.started)
        spec, slot = p.spec, p.spec['slot']
        layer = p.layer.copy()
        progress = ease(age/.6)
        alpha = progress * exit_alpha
        if p.duration and slot != 'stinger': alpha *= min(1., max(0., p.duration-age)/.4)
        dx = dy = 0
        if slot == 'screen':
            if spec['motion'] == 'split':
                dx = int((1-progress)*-self.w)
            else:
                dy = int((1-progress)*40)
            # Reusable motion shapes; text was prepared before the cue was accepted.
            decoration = ImageDraw.Draw(layer)
            for n in range(10):
                x = int((n*83 + age*18) % (self.w+50)) - 25
                y = int(25+n*31 + math.sin(age+n)*7) % self.h
                decoration.ellipse((x, y, x+4, y+4), fill='#CCD1BF' if spec['id'] != 'spotlight' else '#718365')
            if spec['id'] != 'matchup':
                logo = self.logo_sizes[90]
                layer.alpha_composite(logo, (275, 67+int(math.sin(age*2)*4)))
            if spec['id'] == 'countdown':
                remaining = max(0, math.ceil(p.duration-age))
                self.write(layer, (320, 274), f'{remaining//60:02d}:{remaining%60:02d}', 42, GREEN, anchor='mm')
        elif slot == 'lower':
            dx = int((1-progress)*-self.w) if spec['motion'] in ('slide', 'split') else 0
            dy = int((1-progress)*60) if spec['motion'] in ('rise', 'pop') else 0
            if spec['motion'] == 'split':
                # The title and subtitle arrive from opposite sides.
                top = layer.crop((0, 0, self.w, 288)); bottom = layer.crop((0, 288, self.w, self.h))
                layer = Image.new('RGBA', frame.size)
                layer.alpha_composite(top, (dx, 0)); layer.alpha_composite(bottom, (-dx, 288)); dx = 0
        elif slot == 'ticker':
            tape = Image.new('RGBA', (self.w-100, 30))
            width = max(1, int(ImageDraw.Draw(tape).textlength(p.title, font=self.font(14)))+80)
            offset = int(age*50) % width
            draw = ImageDraw.Draw(tape)
            for x in range(-offset, self.w+width, width): draw.text((x, 6), p.title, font=self.font(14), fill=PAPER)
            layer.alpha_composite(tape, (100, 330)); dy = int((1-progress)*32)
        elif slot == 'banner':
            dy = int((1-progress)*-110) if spec['id'] != 'wide-banner' else int((1-progress)*110)
        elif slot == 'bug' and spec['id'] == 'status-bug':
            ImageDraw.Draw(layer).rounded_rectangle((494, 48, 610, 71), 4, fill=GREEN)
            self.write(layer, (552, 59), 'LIVE' if mode == 'LIVE' else 'REPLAY' if mode == 'REPLAY' else 'STANDBY', 10, PAPER, anchor='mm')
        elif slot == 'score':
            if not preview and mode != 'LIVE': return False, False
            wide = spec['id'] == 'score-wide'; x, y, width = (40, 66, 560) if wide else (28, 65, 430)
            value = self.clock(score, now)
            self.write(layer, (x+width//2+10, y+39), value, 14, '#C3CEB6', width=155, family='dmsans')
            flip = max(0, 1-(now-self.score_changed)/.9) if not preview else 0
            if flip:
                draw = ImageDraw.Draw(layer)
                for key_, sx in [('home_score', x+width//2-50), ('away_score', x+width-50)]:
                    if self.previous_score[key_] != score[key_]:
                        draw.rounded_rectangle((sx-7, y+7, sx+43, y+48), 5, outline=GOLD, width=2)
                        # Reveal the new digit below the old digit, then settle the highlight.
                        split_y = y+8+int((1-flip)*37)
                        old = Image.new('RGBA', frame.size)
                        ImageDraw.Draw(old).rectangle((sx-6, y+8, sx+42, y+48), fill=INK)
                        self.write(old, (sx, y+13), '—' if self.previous_score[key_] is None else str(self.previous_score[key_]), 28, GOLD, width=44)
                        layer.alpha_composite(old.crop((sx-6, split_y, sx+43, y+49)), (sx-6, split_y))
                        draw.line((sx-6, y+8+int((1-flip)*37), sx+42, y+8+int((1-flip)*37)), fill=GOLD, width=3)
        elif slot == 'stinger':
            t = min(1, age/2)
            if spec['motion'] == 'wipe':
                dx = int(-self.w+self.w*ease(t/.4)) if t < .4 else int(self.w*ease((t-.6)/.4)) if t > .6 else 0
                layer.alpha_composite(self.logo_sizes[90], (275, 67))
            elif spec['motion'] == 'ribbons':
                layer = Image.new('RGBA', frame.size); draw = ImageDraw.Draw(layer)
                for n, color in enumerate((GREEN, GOLD, PAPER, '#C87732')):
                    shift = int((-1.4+2.8*t)*self.w+n*65)
                    draw.polygon([(shift-70, 0), (shift+105, 0), (shift-35, self.h), (shift-210, self.h)], fill=color)
            elif spec['motion'] == 'confetti':
                layer = Image.new('RGBA', frame.size); draw = ImageDraw.Draw(layer)
                for n in range(42):
                    x = int(self.w/2 + math.cos(n*2.4)*t*self.w*.65)
                    y = int(self.h/2 + math.sin(n*2.4)*t*self.h*.65 + t*t*90)
                    draw.rounded_rectangle((x, y, x+5+n%5, y+4+n%4), 2, fill=(GOLD, GREEN, '#C87732')[n%3])
                alpha *= max(0, (1-t)*2)
            else:
                layer.alpha_composite(self.logo_sizes[90], (275, 67))
                mask = Image.new('L', frame.size); r = int((1-ease(t))*750)
                ImageDraw.Draw(mask).ellipse((320-r, 180-r, 320+r, 180+r), fill=255)
                layer.putalpha(mask)
            if age >= 2: return False, False
        if alpha < 1: layer.putalpha(layer.getchannel('A').point(lambda a: int(a*alpha)))
        channel = layer.getchannel('A'); bounds = channel.getbbox()
        visible = bool(bounds and bounds[0]+dx < self.w and bounds[2]+dx > 0 and bounds[1]+dy < self.h and bounds[3]+dy > 0)
        if visible: frame.alpha_composite(layer, (dx, dy))
        # Opaque screen coverage is recorded with the frame submission acknowledgement.
        covers = slot in ('screen', 'stinger') and dx == 0 and dy == 0 and channel.getextrema()[0] == 255
        return visible, covers

    def compose(self, image, now, mode):
        visible, covering = [], False
        frame = image.convert('RGBA')
        for slot in ('screen', 'score', 'lower', 'ticker', 'banner', 'bug', 'stinger'):
            cue = self.active.get(slot)
            if cue and cue.prepared.duration and now-cue.started >= cue.prepared.duration:
                self.active.pop(slot); cue = None
            if cue and slot == 'stinger' and now-cue.started >= 2:
                self.active.pop(slot); cue = None
            # Current official facts must not be presented over historical replay video.
            has_screen = ('screen' in self.active or 'screen' in self.retiring) and mode != 'REPLAY'
            suppressed = (mode == 'REPLAY' and slot != 'bug') or (has_screen and slot not in ('screen', 'bug', 'stinger')) or (slot=='score' and not self.official_eligible)
            if cue and not suppressed:
                score = self.score if slot == 'score' else cue.prepared.score or self.score
                painted, covers = self.paint(frame, cue, now, mode, score)
                covering |= covers
                if painted: visible.append(cue.summary())
            retiring = self.retiring.get(slot)
            if retiring:
                old, ended = retiring; fade = 1-(now-ended)/.35
                if fade <= 0: self.retiring.pop(slot)
                elif not suppressed:
                    painted, covers = self.paint(frame, old, now, mode, self.score, exit_alpha=fade)
                    covering |= covers
                    if painted: visible.append({**old.summary(), 'exiting': True})
        return frame.convert('RGB'), {'visible': visible, 'covers_camera': covering,
                                      'clock': self.clock(self.score, now), 'score_hidden_during_replay': mode == 'REPLAY',
                                      'score': self.score.copy() if any(cue['slot'] == 'score' for cue in visible) else None}

    def preview(self, prepared):
        if prepared.op != 'cue': raise ValueError('Preview a graphics preset')
        image = Image.new('RGBA', (self.w, self.h), PAPER if prepared.spec['slot'] in ('screen', 'stinger') else '#C4CDB9')
        self.paint(image, Cue('preview', prepared, 0), 1.2, 'LIVE', prepared.score, preview=True)
        return png(image.convert('RGB'))

    def manifest(self):
        return {'schema_version': 1, 'package_id': 'breadcast-default-v1', 'ready': True,
                'width': self.w, 'height': self.h, 'context_revision': None,
                'context': 'neutral Breadcast defaults; event context not bound',
                'score_authority': 'unknown until operator confirmation',
                'resources': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (ROOT / 'fonts' / 'outfit.ttf', ROOT / 'fonts' / 'dmsans.ttf', ROOT / 'brand' / 'breadcast-mark.png')},
                'assets': [{**spec, 'preview_sha256': hashlib.sha256(self.thumbnails[spec['id']]).hexdigest(),
                            'bindings': ['title', 'subtitle'] if spec['slot'] != 'score' else ['home', 'away', 'home_score', 'away_score', 'period', 'clock_seconds']}
                           for spec in CATALOG.values()]}

    def prepare_package(self, event):
        """Build and preload a complete event revision away from media locks."""
        title=(event.title or 'Live event').strip()
        if any(ord(c)<32 for c in title):raise ValueError('Event title must be plain text')
        title=title if len(title)<=120 else title[:119]+'…'
        bindings={
            'opening':(title,'Welcome to the event.'),
            'corner-label':(event.profile.capitalize(),'Live event'),
            'lower-classic':(title,''),
            'break':('Please stand by.',title),
            'closing':('Thanks for watching.',title),
            'brand-bug':('breadcast.',''),
        }
        prepared={};assets=[]
        for key,(heading,subtitle) in bindings.items():
            command=self.prepare({'op':'cue','preset':key,'title':heading,'subtitle':subtitle,'duration_s':4})
            if command.layer.size!=(self.w,self.h) or command.layer.mode!='RGBA':
                raise ValueError('Event asset dimensions or alpha are invalid')
            preview=self.preview(command)
            prepared[key]=command
            assets.append({'id':key,'sha256':hashlib.sha256(png(command.layer)).hexdigest(),
                'preview_sha256':hashlib.sha256(preview).hexdigest(),'title':heading,'subtitle':subtitle})
        # REPLAY is burned into validated replay assets; holding uses this preloaded card.
        assets.append({'id':'replay-label','text':'REPLAY','sha256':hashlib.sha256(b'REPLAY').hexdigest()})
        return {'prepared':prepared,'manifest':{'ready':True,'context_revision':event.revision,
            'width':self.w,'height':self.h,'assets':assets,'preloaded':True,
            'branding':'neutral Breadcast templates','optional_logo':'not supplied or unsupported; neutral fallback',
            'resources':self.manifest()['resources']}}

    def activate_package(self, package):
        self.event_package=package

    def prepared_graphic(self, preset, duration_s):
        from dataclasses import replace
        package=self.event_package
        if not package or preset not in package['prepared']:raise ValueError('Event graphic is not prepared')
        return replace(package['prepared'][preset],duration=duration_s)
