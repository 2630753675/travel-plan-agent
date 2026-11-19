import pathlib
import re

root = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-proposals")

# ---------- K: 曲线按 0-100 重新标定，补右轴，修标签重叠 ----------
p = root / "11-fatigue-curve.html"
t = p.read_text(encoding="utf-8")

svg_start = t.index('<svg class="chart"')
svg_end = t.index('</svg>', svg_start) + len('</svg>')
new_svg = '''<svg class="chart" viewBox="0 0 980 300" preserveAspectRatio="none" role="img" aria-label="五日体力与累计花费曲线">
            <defs>
              <linearGradient id="fadeBlue" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#2f6fed" stop-opacity=".55"/>
                <stop offset="100%" stop-color="#2f6fed" stop-opacity="0"/>
              </linearGradient>
            </defs>
            <rect class="zone" x="0" y="54" width="980" height="65" rx="6"/>
            <text class="axis-label" x="10" y="50">疲劳预警 70</text>
            <g>
              <line class="gridline" x1="70" y1="108" x2="960" y2="108"/>
              <line class="gridline" x1="70" y1="162" x2="960" y2="162"/>
              <line class="gridline" x1="70" y1="216" x2="960" y2="216"/>
              <line class="gridline" x1="70" y1="270" x2="960" y2="270"/>
            </g>
            <text class="axis-label" x="30" y="112">75</text>
            <text class="axis-label" x="30" y="166">50</text>
            <text class="axis-label" x="30" y="220">25</text>
            <text class="axis-label" x="36" y="274">0</text>
            <text class="axis-label" x="10" y="288" style="font-size:9px">体力</text>

            <text class="axis-label" x="944" y="220">2k</text>
            <text class="axis-label" x="944" y="166">4k</text>
            <text class="axis-label" x="944" y="112">6k</text>
            <text class="axis-label" x="944" y="60">8k</text>
            <text class="axis-label" x="912" y="288" style="font-size:9px">花费</text>

            <text class="axis-label" x="120" y="290">11-20</text>
            <text class="axis-label" x="290" y="290">11-21</text>
            <text class="axis-label" x="460" y="290">11-22</text>
            <text class="axis-label" x="630" y="290">11-23</text>
            <text class="axis-label" x="800" y="290">11-24</text>

            <path class="fatigue-area" d="M120 158 L290 184 L460 201 L630 173 L800 106 L800 270 L120 270 Z"/>
            <path class="fatigue" d="M120 158 L290 184 L460 201 L630 173 L800 106"/>
            <circle class="dot" cx="120" cy="158" r="4.5"/>
            <circle class="dot" cx="290" cy="184" r="4.5"/>
            <circle class="dot" cx="460" cy="201" r="4.5"/>
            <circle class="dot" cx="630" cy="173" r="4.5"/>
            <circle class="dot warn" cx="800" cy="106" r="5"/>
            <text class="axis-label" x="96" y="148">52</text>
            <text class="axis-label" x="266" y="174">40</text>
            <text class="axis-label" x="436" y="191">32</text>
            <text class="axis-label" x="606" y="163">45</text>
            <text class="axis-label" x="778" y="96" style="fill:#b4761a">76</text>

            <path class="cost" d="M120 193 L290 166 L460 141 L630 114 L800 55"/>
            <text class="axis-label" x="806" y="50" style="fill:#b4761a">7,960</text>

            <circle class="rest" cx="205" cy="171" r="3"/>
            <circle class="rest" cx="375" cy="192" r="3"/>
            <circle class="rest" cx="545" cy="187" r="3"/>
            <circle class="rest" cx="715" cy="139" r="3"/>
            <text class="axis-label" x="176" y="164" style="fill:#199a6b">午休</text>
            <text class="axis-label" x="516" y="180" style="fill:#199a6b">午休</text>
          </svg>'''
t = t[:svg_start] + new_svg + t[svg_end:]

# 标签与事实对齐：11-22 才是最省体力
t = t.replace('<span class="tag2">最轻松</span>\n          <div class="d">11-21</div>',
              '<span class="tag2">两处相距 5km</span>\n          <div class="d">11-21</div>')
t = t.replace('<span class="tag2 warn">雨天</span>\n          <div class="d">11-22</div>',
              '<span class="tag2">雨天 · 最省体力</span>\n          <div class="d">11-22</div>')
p.write_text(t, encoding="utf-8")

# ---------- J: 长卷在宽屏铺满，题跋/说明保持窄栏 ----------
p = root / "10-handscroll.html"
t = p.read_text(encoding="utf-8")
t = t.replace(".page{max-width:1280px;margin:0 auto;padding:26px 20px 60px}",
              ".page{max-width:2520px;margin:0 auto;padding:26px 20px 60px}\n"
              "  .rule,.caption,.colophon{max-width:1240px;margin-left:auto;margin-right:auto}\n"
              "  .colophon{margin-top:26px}")
p.write_text(t, encoding="utf-8")
print("patched K and J")
