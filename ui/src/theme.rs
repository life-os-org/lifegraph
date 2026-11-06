//! Black, white and grey. Liberation Sans for text, Berkeley Mono for data and labels. No rounding, 1px lines, inverted selection.
use eframe::egui::{self, Color32, FontData, FontDefinitions, FontFamily, FontId, Stroke, TextStyle};
use std::sync::Arc;

pub const BG: Color32 = Color32::from_rgb(0, 0, 0);
pub const PANEL: Color32 = Color32::from_rgb(6, 6, 6);
pub const LINE: Color32 = Color32::from_rgb(44, 44, 44);
pub const LINE_HI: Color32 = Color32::from_rgb(120, 120, 120);
pub const INK: Color32 = Color32::from_rgb(232, 232, 232);
pub const DIM: Color32 = Color32::from_rgb(128, 128, 128);
pub const FAINT: Color32 = Color32::from_rgb(70, 70, 70);
pub const WHITE: Color32 = Color32::from_rgb(255, 255, 255);
pub const ALERT: Color32 = Color32::from_rgb(242, 69, 56);

pub fn sans_bold() -> FontFamily { FontFamily::Name("sans-bold".into()) }
pub fn mono_bold() -> FontFamily { FontFamily::Name("mono-bold".into()) }
pub fn mono(size: f32) -> FontId { FontId::new(size, FontFamily::Monospace) }
pub fn mono_bold_id(size: f32) -> FontId { FontId::new(size, mono_bold()) }
pub fn label(size: f32) -> FontId { FontId::new(size, sans_bold()) }

pub fn install(ctx: &egui::Context) {
    let mut fonts = FontDefinitions::default();
    let add = |fonts: &mut FontDefinitions, name: &str, bytes: &'static [u8]| {
        fonts.font_data.insert(name.to_owned(), Arc::new(FontData::from_static(bytes)));
    };
    add(&mut fonts, "liberation", include_bytes!("../assets/fonts/LiberationSans-Regular.ttf"));
    add(&mut fonts, "liberation-bold", include_bytes!("../assets/fonts/LiberationSans-Bold.ttf"));
    fonts.families.entry(FontFamily::Proportional).or_default().insert(0, "liberation".to_owned());
    fonts.families.insert(sans_bold(), vec!["liberation-bold".to_owned(), "liberation".to_owned()]);

    #[cfg(not(no_berkeley))]
    {
        add(&mut fonts, "berkeley", include_bytes!("../assets/fonts/BerkeleyMono-Regular.otf"));
        add(&mut fonts, "berkeley-bold", include_bytes!("../assets/fonts/BerkeleyMono-Bold.otf"));
        fonts.families.entry(FontFamily::Monospace).or_default().insert(0, "berkeley".to_owned());
        let mut mb = vec!["berkeley-bold".to_owned(), "berkeley".to_owned()];
        mb.extend(fonts.families.get(&FontFamily::Monospace).cloned().unwrap_or_default().into_iter().filter(|n| n != "berkeley"));
        fonts.families.insert(mono_bold(), mb);
    }
    #[cfg(no_berkeley)]
    {
        let fallback = fonts.families.get(&FontFamily::Monospace).cloned().unwrap_or_default();
        fonts.families.insert(mono_bold(), fallback);
    }
    ctx.set_fonts(fonts);

    ctx.all_styles_mut(|style| {
        style.text_styles = [
            (TextStyle::Heading, label(15.0)),
            (TextStyle::Body, FontId::new(14.0, FontFamily::Proportional)),
            (TextStyle::Button, mono(12.0)),
            (TextStyle::Small, mono(10.5)),
            (TextStyle::Monospace, mono(12.5)),
        ].into();
        let v = &mut style.visuals;
        *v = egui::Visuals::dark();
        v.override_text_color = Some(INK);
        v.window_fill = PANEL;
        v.panel_fill = PANEL;
        v.extreme_bg_color = BG;
        v.faint_bg_color = Color32::from_rgb(12, 12, 12);
        v.window_stroke = Stroke::new(1.0, LINE);
        v.window_corner_radius = 0.0.into();
        v.menu_corner_radius = 0.0.into();
        v.window_shadow = egui::Shadow::NONE;
        v.popup_shadow = egui::Shadow::NONE;
        v.selection.bg_fill = WHITE;
        v.selection.stroke = Stroke::new(1.0, BG);
        v.hyperlink_color = WHITE;
        let w = &mut v.widgets;
        for (wv, fill, stroke, fg) in [
            (&mut w.noninteractive, PANEL, LINE, INK),
            (&mut w.inactive, BG, LINE_HI, INK),
            (&mut w.hovered, WHITE, WHITE, BG),
            (&mut w.active, INK, INK, BG),
            (&mut w.open, WHITE, WHITE, BG),
        ] {
            wv.bg_fill = fill;
            wv.weak_bg_fill = fill;
            wv.bg_stroke = Stroke::new(1.0, stroke);
            wv.fg_stroke = Stroke::new(1.0, fg);
            wv.corner_radius = 0.0.into();
            wv.expansion = 0.0;
        }
        style.spacing.item_spacing = egui::vec2(8.0, 6.0);
        style.spacing.button_padding = egui::vec2(10.0, 4.0);
        style.spacing.window_margin = egui::Margin::same(12);
        style.spacing.scroll.bar_width = 4.0;
        style.spacing.scroll.floating = true;
    });
}
