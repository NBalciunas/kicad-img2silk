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
4) Restart KiCad. The plugin is now installed.

## Usage

![Img2Silk menu](docs/example-1.png)

### Importing an image

1) Click **Import Image** to load the artwork you want to convert.
2) Pick a **Dithering algorithm**: **Threshold** for logos and line art, or Atkinson / Bayer 8×8 / Floyd–Steinberg / Random to reproduce photos and gradients as dots.
3) Use the **Black / white threshold** slider to control the conversion (in dithering modes it acts as a brightness fine-tune).
4) In dithering modes, set the **Pixel Scale**, the physical size of each dot in mm (smaller = more detail, but more polygons).
5) Choose the target **Layer**: Copper, Silkscreen, or Solder Mask.
6) Enable **Invert colors** if the result comes out inverted (black/white swapped) from what you want.
7) Enter the desired **Length (mm)** and **Width (mm)** for the final graphic.
8) Keep **Maintain aspect ratio** checked to scale proportionally, or uncheck it to set width and height independently.
9) Click **Insert Graphics** to generate the graphic. It is placed as a single grouped item you can move or delete as one.

### Placing with a reference frame

1) Instead of typing the size, click **Place Reference Frame**. A rectangle appears on the board (on the Dwgs.User layer) and the dialog stays open.
2) Move and resize the rectangle with the normal KiCad tools by dragging its corners. The **Length** and **Width** fields track the frame live, and the preview updates when you finish dragging.
3) Keep **Maintain aspect ratio** checked to have the frame snap back to the image's aspect ratio after each resize, or uncheck it to size the frame freely.
4) Click back into the dialog and press **Insert Graphics**. The graphic is generated exactly inside the frame, and the frame is removed automatically.

### Resizing or editing a placed graphic

1) Select the generated group on the board.
2) Run Img2Silk again. The dialog reopens with the original image and all settings pre-filled.
3) Change the size by typing new dimensions or by scaling a **Place Reference Frame** rectangle, and adjust any other settings.
4) Click **Update Graphics**. The old graphic is replaced in place.

#### Side note: The source image file must still exist at its original location.

## Example

The following example shows a generated silkscreen footprint using the specified parameters from the **Usage** section.

![Imported image in 2D view](docs/example-2.png)
![Imported image in 3D view](docs/example-3.png)

This illustrates how the silkscreen is created in both 2D and 3D views, ready for placement on the PCB.

## License

This project is licensed under the MIT License.
Copyright © 2026 Nojus Balčiūnas