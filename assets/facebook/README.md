# Facebook Jobs visual assets

Runtime templates:

- `job-new-orange.png`
- `job-deadline-yellow.png`
- `job-alert-blue.png`
- `job-apply-red.png`

The source artwork stays unchanged. The renderer normalizes each 1254×1254
template to a 1080×1350 (4:5) Facebook feed card without stretching the frame,
logo, side icon or footer.

## Jobs visual hierarchy

The generated card follows one fixed hierarchy across all four templates:

1. Cybero+ template branding remains untouched.
2. A verified employer logo is rendered large in the upper-middle area.
3. If no verified logo exists, the verified employer name is rendered as text;
   a random or unverified logo is never substituted.
4. The image title is a concise role-first Facebook visual title, not the full
   SEO headline. Employer and location are removed when they are already
   communicated elsewhere.
5. The full SEO/job headline remains available in the Facebook caption/article.

## Readability rules

- Font family: Firjar ExtraBold, Arabic + Latin/French.
- Final canvas: 1080×1350.
- Job title minimum size: 48 px.
- Job title maximum size: 88 px.
- Maximum title lines: 4.
- Firjar width axis may tighten from 100 to 90 before reducing the font further.
- No title ellipsis is used to force an unreadable layout.
- The title stays inside the shared safe zone used by all four side icons.
- Employer logos are cropped to their visible mark before sizing.
- Horizontal, square and vertical logos use different maximum boxes so each
  remains visually large without distortion.
- Header, right-side icon and footer are protected by explicit safe areas.

The Jobs visual tests render Arabic, French, mixed-language, short and long
titles against every template and reject layouts that cross the safe zone,
drop below the minimum font size or exceed the line limit.
