# Vertex Shadow Painter for Blender

A Blender add-on (Blender 3.0+ & 4.x/5.x compatible) that automatically computes and paints directional shadows, terminator shading, and ambient occlusion directly into **Vertex Colors / Color Attributes**.

---

## Installation in Blender

1. Open Blender.
2. Go to **Edit > Preferences...**
3. Select the **Add-ons** tab on the left.
4. Click **Install...** (or the down-arrow next to install in Blender 4.2+).
5. Navigate to:
   ```
   Tools/blender_addons/vertex_shadow_painter.py
   ```
6. Check the checkbox next to **"Paint: Vertex Shadow Painter"** to enable it.

---

## How to Use

1. **Select Mesh(es)**: Select one or more mesh objects in the 3D Viewport.
2. **Open the Sidebar**: Press `N` in the 3D Viewport to open the sidebar.
3. Click on the **Vertex Shadow** tab.
4. Configure your lighting and shadow options:
   - **Light Source**: Choose between **Light Object** (select a Sun/Light from your scene) or **Manual Angles** (adjust Azimuth and Elevation sliders).
   - **Direct Shadow**: Enables raycasted occlusion shadows (shadows cast by roofs, cliffs, overhangs, wheels, etc.).
   - **Terminator Shading**: Automatically darkens vertices that face away from the light source (\(N \cdot L\)). Choose between *Lambert (sharp)*, *Half-Lambert (soft)*, or *Smooth Step*.
   - **Ambient Occlusion (Contact / Crevices)**: Casts hemispherical rays around each vertex normal to darken crevices, undercarriages, and ground contact points.
   - **Shadow Darkness & Tint**: Configure how dark the shadow is and give it an optional stylized tint (e.g. dark blue/slate).
   - **Blend Mode**:
     - `Multiply`: Darkens existing vertex colors while preserving existing painted artwork.
     - `Replace`: Paints fresh shadow colors onto the mesh.
     - `Darken Only`: Keeps the minimum (darkest) value between existing colors and calculated shadow.
5. Click **"Paint / Bake Vertex Shadows"**.

---

## Viewing Vertex Colors in Blender Viewport

To see the painted vertex shadows in Blender:
1. In the 3D Viewport, click the **Viewport Shading** dropdown arrow in the top right.
2. Under **Color**, select **Attribute** (or **Vertex**).
3. Alternatively, switch the 3D Viewport mode to **Vertex Paint**.

---

## Exporting to Godot / GLTF

When exporting to `.glb` or `.gltf` for Godot:
1. Go to **File > Export > glTF 2.0 (.glb/.gltf)**.
2. In the export settings under **Data > Mesh**, ensure **Color Attributes** (or **Vertex Colors**) is enabled.
3. In Godot, on your `StandardMaterial3D`, turn on **Vertex Color > Use As Albedo** (or multiply in your shader) to view the baked shadows!
