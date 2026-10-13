#pragma once
// UI text rasterisation (stb_truetype), port of create_text_texture(): centred
// multi-line text on a solid background, shrinking the font by 0.85 steps
// until it fits inside the margins. Fonts: IBM Plex Sans Bold (buttons) and
// Bold Italic (labels), the Project Babble brand typeface.

#include <array>
#include <cstdint>
#include <string>
#include <vector>

using Rgb = std::array<uint8_t, 3>;

class TextRasterizer {
public:
    bool load(const std::string& font_dir);  // false: no fonts found (text renders blank)
    // RGBA8 pixels, row 0 at the top
    std::vector<uint8_t> render(const std::string& text, int width, int height, Rgb bg, Rgb fg,
                                bool italic, int margin_x, int margin_y) const;

private:
    struct Font;
    std::vector<uint8_t> bold_data_, italic_data_;
};
