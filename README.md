<img align="right" width="60px" src="plugins/assets/icon.png">

# Img2Silk

## Features

- Import and convert images to silkscreen, copper, or solder mask directly in KiCad.
- Five conversion algorithms: Atkinson, Bayer 8×8, Floyd–Steinberg, and Random dithering for photos and gradients, plus Threshold for logos and line art.
- Live preview that shows exactly how the image will look on the board.
- Adjust size, black / white threshold, dither dot size, and other settings before placing the image.
- No external image editor required.

## Installation

1) Download the [latest release ZIP file](https://github.com/NBalciunas/kicad-img2silk/releases).
2) Open KiCad and in the main window click on "Plugin and Content Manager".
3) Click "Install from File..." and select the downloaded ZIP file.  
4) Restart KiCad — the plugin is now installed.

## Usage

![Img2Silk menu](docs/example-1.png)

1) Click **Import Image** to load the artwork you want to convert.
2) Pick a **Dithering algorithm**: **Threshold** for logos and line art, or Atkinson / Bayer 8×8 / Floyd–Steinberg / Random to reproduce photos and gradients as dots.
3) Use the **Black / white threshold** slider to control the conversion (in dithering modes it acts as a brightness fine-tune).
4) In dithering modes, set the **Pixel Scale**, the physical size of each dot in mm (smaller = more detail, but more polygons).
5) Choose the target **Layer**: Copper, Silkscreen, or Solder Mask (drawing on the mask layer *opens* the mask, exposing what is underneath).
6) Enable **Invert colors** if the result comes out inverted (black/white swapped) from what you want.
7) Enter the desired **Length (mm)** and **Width (mm)** for the final graphic.
8) Keep **Maintain aspect ratio** checked to scale proportionally, or uncheck it to set width and height independently.
9) Click **Insert Graphics** to generate the graphic. It is placed as a single grouped item you can move or delete as one.

## Example

The following example shows a generated silkscreen footprint using the specified parameters from the **Usage** section.

![Imported image in 2D view](docs/example-2.png)
![Imported image in 3D view](docs/example-3.png)

This illustrates how the silkscreen is created in both 2D and 3D views, ready for placement on the PCB.

## Future

- Resizing the placed graphic directly on the board.

## License

This project is licensed under the MIT License.
Copyright © 2026 Nojus Balčiūnas