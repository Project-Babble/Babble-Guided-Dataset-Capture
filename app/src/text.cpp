#include "text.h"

#include "common.h"

#include "stb_truetype.h"

#include <algorithm>
#include <cmath>
#include <fstream>

namespace {

std::vector<uint8_t> read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), {});
}

std::vector<uint32_t> decode_utf8(const std::string& s) {
    std::vector<uint32_t> out;
    for (size_t i = 0; i < s.size();) {
        uint8_t c = uint8_t(s[i]);
        uint32_t cp;
        int extra;
        if (c < 0x80) { cp = c; extra = 0; }
        else if ((c >> 5) == 6) { cp = c & 0x1F; extra = 1; }
        else if ((c >> 4) == 14) { cp = c & 0x0F; extra = 2; }
        else if ((c >> 3) == 30) { cp = c & 0x07; extra = 3; }
        else { ++i; continue; }
        if (i + size_t(extra) >= s.size() && extra > 0) break;  // truncated sequence
        for (int k = 1; k <= extra; ++k) cp = (cp << 6) | (uint8_t(s[i + size_t(k)]) & 0x3F);
        out.push_back(cp);
        i += size_t(extra) + 1;
    }
    return out;
}

struct Canvas {
    int width = 0, height = 0;
    std::vector<float> coverage;
    int min_x = 1 << 30, min_y = 1 << 30, max_x = -(1 << 30), max_y = -(1 << 30);
};

}  // namespace

bool TextRasterizer::load(const std::string& font_dir) {
    bold_data_ = read_file(font_dir + "/IBMPlexSans-Bold.ttf");
    italic_data_ = read_file(font_dir + "/IBMPlexSans-BoldItalic.ttf");
    if (italic_data_.empty()) italic_data_ = bold_data_;
    if (bold_data_.empty()) bold_data_ = italic_data_;
    if (bold_data_.empty()) {
        LOG_ERR("No UI fonts in %s; dialog text will be blank", font_dir.c_str());
        return false;
    }
    return true;
}

std::vector<uint8_t> TextRasterizer::render(const std::string& text, int width, int height, Rgb bg, Rgb fg,
                                            bool italic, int margin_x, int margin_y) const {
    std::vector<uint8_t> rgba(size_t(width) * size_t(height) * 4);
    for (size_t i = 0; i < rgba.size(); i += 4) {
        rgba[i] = bg[0];
        rgba[i + 1] = bg[1];
        rgba[i + 2] = bg[2];
        rgba[i + 3] = 255;
    }
    const std::vector<uint8_t>& data = italic ? italic_data_ : bold_data_;
    if (data.empty()) return rgba;
    stbtt_fontinfo font;
    if (!stbtt_InitFont(&font, data.data(), stbtt_GetFontOffsetForIndex(data.data(), 0))) return rgba;

    std::vector<std::vector<uint32_t>> lines(1);
    for (uint32_t cp : decode_utf8(text)) {
        if (cp == '\n') lines.emplace_back();
        else lines.back().push_back(cp);
    }

    int ascent, descent, gap;
    stbtt_GetFontVMetrics(&font, &ascent, &descent, &gap);

    // Lay out at a font size into a coverage canvas; returns ink bounds
    auto layout = [&](int size, Canvas& canvas) {
        float scale = stbtt_ScaleForMappingEmToPixels(&font, float(size));
        int spacing = int(size * 0.4);
        float line_advance = ascent * scale + float(spacing);  // like Pillow's multiline spacing
        std::vector<float> widths;
        for (const auto& line : lines) {
            float w = 0;
            for (size_t i = 0; i < line.size(); ++i) {
                int adv, lsb;
                stbtt_GetCodepointHMetrics(&font, int(line[i]), &adv, &lsb);
                w += adv * scale;
                if (i + 1 < line.size()) w += stbtt_GetCodepointKernAdvance(&font, int(line[i]), int(line[i + 1])) * scale;
            }
            widths.push_back(w);
        }
        float widest = *std::max_element(widths.begin(), widths.end());
        canvas.width = int(std::ceil(widest)) + size * 2;
        canvas.height = int(std::ceil(line_advance * float(lines.size()))) + size * 2;
        canvas.coverage.assign(size_t(canvas.width) * size_t(canvas.height), 0.0f);
        for (size_t li = 0; li < lines.size(); ++li) {
            float x = float(size) + (widest - widths[li]) / 2;  // align="center"
            float baseline = float(size) + float(li) * line_advance + ascent * scale;
            const auto& line = lines[li];
            for (size_t i = 0; i < line.size(); ++i) {
                int cp = int(line[i]);
                int x0, y0, x1, y1;
                float shift = x - std::floor(x);
                stbtt_GetCodepointBitmapBoxSubpixel(&font, cp, scale, scale, shift, 0, &x0, &y0, &x1, &y1);
                int gw = x1 - x0, gh = y1 - y0;
                if (gw > 0 && gh > 0) {
                    std::vector<uint8_t> glyph(size_t(gw) * size_t(gh));
                    stbtt_MakeCodepointBitmapSubpixel(&font, glyph.data(), gw, gh, gw, scale, scale, shift, 0, cp);
                    int ox = int(std::floor(x)) + x0, oy = int(std::lround(baseline)) + y0;
                    for (int gy = 0; gy < gh; ++gy) {
                        for (int gx = 0; gx < gw; ++gx) {
                            uint8_t v = glyph[size_t(gy) * size_t(gw) + size_t(gx)];
                            int cx = ox + gx, cy = oy + gy;
                            if (!v || cx < 0 || cy < 0 || cx >= canvas.width || cy >= canvas.height) continue;
                            float& c = canvas.coverage[size_t(cy) * size_t(canvas.width) + size_t(cx)];
                            c = std::min(1.0f, c + v / 255.0f);
                            canvas.min_x = std::min(canvas.min_x, cx);
                            canvas.max_x = std::max(canvas.max_x, cx + 1);
                            canvas.min_y = std::min(canvas.min_y, cy);
                            canvas.max_y = std::max(canvas.max_y, cy + 1);
                        }
                    }
                }
                int adv, lsb;
                stbtt_GetCodepointHMetrics(&font, cp, &adv, &lsb);
                x += adv * scale;
                if (i + 1 < line.size()) x += stbtt_GetCodepointKernAdvance(&font, cp, int(line[i + 1])) * scale;
            }
        }
    };

    int size = int(height * 0.5);
    Canvas canvas;
    while (true) {
        canvas = Canvas{};
        layout(size, canvas);
        int ink_w = canvas.max_x - canvas.min_x, ink_h = canvas.max_y - canvas.min_y;
        if (size <= 14 || ink_w <= 0 || (ink_w <= width - margin_x && ink_h <= height - margin_y)) break;
        size = int(size * 0.85);
    }
    if (canvas.max_x <= canvas.min_x) return rgba;  // blank text

    // Centre the ink box on the texture and composite fg over bg
    int ink_w = canvas.max_x - canvas.min_x, ink_h = canvas.max_y - canvas.min_y;
    int dx = (width - ink_w) / 2 - canvas.min_x, dy = (height - ink_h) / 2 - canvas.min_y;
    for (int y = canvas.min_y; y < canvas.max_y; ++y) {
        int ty = y + dy;
        if (ty < 0 || ty >= height) continue;
        for (int x = canvas.min_x; x < canvas.max_x; ++x) {
            int tx = x + dx;
            if (tx < 0 || tx >= width) continue;
            float a = canvas.coverage[size_t(y) * size_t(canvas.width) + size_t(x)];
            if (a <= 0) continue;
            uint8_t* px = &rgba[(size_t(ty) * size_t(width) + size_t(tx)) * 4];
            for (int c = 0; c < 3; ++c) px[c] = uint8_t(std::lround(bg[size_t(c)] + (fg[size_t(c)] - bg[size_t(c)]) * a));
        }
    }
    return rgba;
}
