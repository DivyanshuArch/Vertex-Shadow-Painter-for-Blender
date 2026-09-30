bl_info = {
    "name": "Vertex Shadow Painter",
    "author": "Antigravity",
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Vertex Shadow",
    "description": "Paints shadows onto vertices using raytracing, normal incidence, and ambient occlusion",
    "warning": "",
    "doc_url": "",
    "category": "Paint",
}

import bpy
import bmesh
import math
import mathutils
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
import time

def get_or_create_color_attribute(mesh, layer_name="Shadow", domain="POINT"):
    """Compatibility wrapper for Blender 3.2+ Color Attributes and older Vertex Colors."""
    # Blender 3.2+
    if hasattr(mesh, "color_attributes"):
        color_attr = mesh.color_attributes.get(layer_name)
        if not color_attr:
            # domain: 'POINT' (per vertex) or 'CORNER' (per face-corner/loop)
            color_attr = mesh.color_attributes.new(
                name=layer_name,
                type='FLOAT_COLOR',
                domain=domain
            )
        mesh.color_attributes.active = color_attr
        return color_attr
    else:
        # Blender < 3.2 fallback
        vcol = mesh.vertex_colors.get(layer_name)
        if not vcol:
            vcol = mesh.vertex_colors.new(name=layer_name)
        mesh.vertex_colors.active = vcol
        return vcol

def build_scene_bvh(scene, exclude_objects=None, evaluate_depsgraph=True):
    """Builds a single merged BVH tree of mesh geometry in world space."""
    if exclude_objects is None:
        exclude_objects = set()

    all_verts = []
    all_polys = []
    vert_offset = 0

    depsgraph = bpy.context.evaluated_depsgraph_get() if evaluate_depsgraph else None

    for obj in scene.objects:
        if obj.type != 'MESH' or obj in exclude_objects or obj.hide_viewport:
            continue

        if evaluate_depsgraph and depsgraph:
            eval_obj = obj.evaluated_get(depsgraph)
            me = eval_obj.to_mesh()
        else:
            me = obj.data

        world_mat = obj.matrix_world
        verts = [world_mat @ v.co for v in me.vertices]
        all_verts.extend(verts)

        for poly in me.polygons:
            poly_indices = [idx + vert_offset for idx in poly.vertices]
            # Triangulate polygons on the fly for BVH
            if len(poly_indices) == 3:
                all_polys.append(poly_indices)
            elif len(poly_indices) == 4:
                all_polys.append([poly_indices[0], poly_indices[1], poly_indices[2]])
                all_polys.append([poly_indices[0], poly_indices[2], poly_indices[3]])
            else:
                for i in range(1, len(poly_indices) - 1):
                    all_polys.append([poly_indices[0], poly_indices[i], poly_indices[i+1]])

        vert_offset += len(verts)

        if evaluate_depsgraph and depsgraph:
            if hasattr(eval_obj, "to_mesh_clear"):
                eval_obj.to_mesh_clear()

    if not all_polys or not all_verts:
        return None

    return BVHTree.FromPolygons(all_verts, all_polys, all_triangles=True, epsilon=0.0001)

def generate_hemisphere_samples(num_samples=8):
    """Generates quasi-uniform hemisphere sample vectors pointing upwards along +Z."""
    samples = []
    for i in range(num_samples):
        # Fibonacci spiral on hemisphere
        phi = 2.0 * math.pi * (i / 1.61803398875)
        cos_theta = 1.0 - (i + 0.5) / float(num_samples)
        sin_theta = math.sqrt(max(0.0, 1.0 - cos_theta * cos_theta))
        x = math.cos(phi) * sin_theta
        y = math.sin(phi) * sin_theta
        z = cos_theta
        samples.append(Vector((x, y, z)).normalized())
    return samples

def align_to_normal(sample_vec, normal):
    """Rotates a sample vector from +Z axis to align with the given surface normal."""
    up = Vector((0.0, 0.0, 1.0))
    if normal.dot(up) > 0.9999:
        return sample_vec
    elif normal.dot(up) < -0.9999:
        return Vector((sample_vec.x, -sample_vec.y, -sample_vec.z))
    
    rot_quat = up.rotation_difference(normal)
    return rot_quat @ sample_vec


class VSP_OT_BakeShadows(bpy.types.Operator):
    """Bake directional and ambient shadows into vertex colors"""
    bl_idname = "paint.vsp_bake_shadows"
    bl_label = "Paint / Bake Vertex Shadows"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.vsp_props
        active_obj = context.active_object
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objs and active_obj and active_obj.type == 'MESH':
            selected_objs = [active_obj]

        if not selected_objs:
            self.report({'WARNING'}, "No mesh objects selected.")
            return {'CANCELLED'}

        start_time = time.time()

        # Determine light direction (unit vector pointing TOWARD light)
        if props.light_mode == 'SUN_OBJECT' and props.light_object:
            light_obj = props.light_object
            # A light's forward axis in Blender is -Z, so light vector pointing to sun is opposite
            light_dir = (light_obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        else:
            azimuth = math.radians(props.light_azimuth)
            elevation = math.radians(props.light_elevation)
            x = math.cos(elevation) * math.sin(azimuth)
            y = -math.cos(elevation) * math.cos(azimuth)
            z = math.sin(elevation)
            light_dir = Vector((x, y, z)).normalized()

        # Build BVH tree
        bvh = None
        if props.cast_direct_shadows or props.enable_ao:
            bvh = build_scene_bvh(context.scene)
            if bvh is None:
                self.report({'WARNING'}, "Could not build collision geometry for shadow raycasting.")

        ao_samples = generate_hemisphere_samples(props.ao_sample_count) if props.enable_ao else []
        total_vertices_painted = 0

        shadow_rgb = Vector((props.shadow_color[0], props.shadow_color[1], props.shadow_color[2]))
        lit_rgb = Vector((props.lit_color[0], props.lit_color[1], props.lit_color[2]))

        for obj in selected_objs:
            mesh = obj.data
            mat_world = obj.matrix_world
            mat_world_inv_trans = mat_world.to_3x3().inverted().transposed()

            color_layer = get_or_create_color_attribute(mesh, props.layer_name, props.layer_domain)
            domain = getattr(color_layer, 'domain', 'POINT')

            if hasattr(mesh, "calc_normals_split"):
                mesh.calc_normals_split()

            if domain == 'POINT':
                count = len(mesh.vertices)
                for i, v in enumerate(mesh.vertices):
                    w_pos = mat_world @ v.co
                    raw_norm = mat_world_inv_trans @ v.normal
                    w_norm = raw_norm.normalized() if raw_norm.length_squared > 0.00001 else Vector((0.0, 0.0, 1.0))

                    shadow_factor = self.calculate_shadow(
                        w_pos, w_norm, light_dir, bvh, props, ao_samples
                    )

                    self.apply_color(color_layer, i, shadow_factor, shadow_rgb, lit_rgb, props)
                total_vertices_painted += count

            elif domain == 'CORNER':
                count = len(mesh.loops)
                for loop_idx, loop in enumerate(mesh.loops):
                    v = mesh.vertices[loop.vertex_index]
                    w_pos = mat_world @ v.co
                    norm = getattr(loop, 'normal', v.normal)
                    raw_norm = mat_world_inv_trans @ norm
                    w_norm = raw_norm.normalized() if raw_norm.length_squared > 0.00001 else Vector((0.0, 0.0, 1.0))

                    shadow_factor = self.calculate_shadow(
                        w_pos, w_norm, light_dir, bvh, props, ao_samples
                    )

                    self.apply_color(color_layer, loop_idx, shadow_factor, shadow_rgb, lit_rgb, props)
                total_vertices_painted += count

            mesh.update()

        elapsed = time.time() - start_time
        self.report({'INFO'}, f"Baked shadows onto {total_vertices_painted} elements in {elapsed:.2f}s")
        return {'FINISHED'}

    def calculate_shadow(self, w_pos, w_norm, light_dir, bvh, props, ao_samples):
        """Calculates final shadow multiplier: 0.0 (full shadow) to 1.0 (fully lit)."""
        lighting = 1.0

        # 1. Terminator / Normal incidence
        if props.enable_terminator:
            n_dot_l = w_norm.dot(light_dir)
            if props.terminator_type == 'LAMBERT':
                t = max(0.0, n_dot_l)
            elif props.terminator_type == 'HALF_LAMBERT':
                t = (n_dot_l * 0.5 + 0.5) ** 1.5
            else: # SMOOTH
                t = (math.sin(max(-1.0, min(1.0, n_dot_l)) * math.pi * 0.5) * 0.5 + 0.5)
            lighting *= t

        # 2. Direct raycast shadow
        if props.cast_direct_shadows and bvh is not None:
            if lighting > 0.001:  # Only raycast if surface is facing light
                ray_origin = w_pos + w_norm * props.shadow_bias
                hit_loc, hit_norm, hit_idx, hit_dist = bvh.ray_cast(ray_origin, light_dir, props.ray_max_distance)
                if hit_loc is not None:
                    # In direct shadow
                    lighting *= (1.0 - props.shadow_strength)

        # 3. Ambient Occlusion (contact & crevice darkening)
        if props.enable_ao and bvh is not None and ao_samples:
            occluded_count = 0
            ray_origin = w_pos + w_norm * props.shadow_bias
            for s in ao_samples:
                dir_sample = align_to_normal(s, w_norm)
                hit_loc, hit_norm, hit_idx, hit_dist = bvh.ray_cast(
                    ray_origin, dir_sample, props.ao_distance
                )
                if hit_loc is not None:
                    occluded_count += 1
            ao_factor = 1.0 - (occluded_count / float(len(ao_samples))) * props.ao_strength
            lighting *= max(0.0, min(1.0, ao_factor))

        return max(0.0, min(1.0, lighting))

    def apply_color(self, color_layer, element_idx, shadow_factor, shadow_rgb, lit_rgb, props):
        """Blends and assigns color according to blend mode."""
        dark_col = shadow_rgb * (1.0 - props.shadow_strength) + lit_rgb * props.shadow_strength * 0.1
        calculated_color = dark_col.lerp(lit_rgb, shadow_factor)

        target = color_layer.data[element_idx]

        if props.blend_mode == 'REPLACE':
            target.color = (calculated_color.x, calculated_color.y, calculated_color.z, 1.0)
        elif props.blend_mode == 'MULTIPLY':
            cur = target.color
            target.color = (
                cur[0] * calculated_color.x,
                cur[1] * calculated_color.y,
                cur[2] * calculated_color.z,
                cur[3]
            )
        elif props.blend_mode == 'DARKEN':
            cur = target.color
            target.color = (
                min(cur[0], calculated_color.x),
                min(cur[1], calculated_color.y),
                min(cur[2], calculated_color.z),
                cur[3]
            )


class VSP_Properties(bpy.types.PropertyGroup):
    layer_name: bpy.props.StringProperty(
        name="Color Layer",
        description="Name of the vertex color / color attribute layer",
        default="Shadow"
    )
    layer_domain: bpy.props.EnumProperty(
        name="Domain",
        description="Vertex color domain (Point = smooth vertex interpolation, Corner = sharp face loops)",
        items=[
            ('POINT', "Point (Vertices)", "Smooth shading across vertices"),
            ('CORNER', "Corner (Face Loops)", "Sharp edges between faces"),
        ],
        default='POINT'
    )
    blend_mode: bpy.props.EnumProperty(
        name="Blend Mode",
        description="How to combine shadows with existing vertex colors",
        items=[
            ('MULTIPLY', "Multiply", "Darken existing vertex colors without overwriting existing paint"),
            ('REPLACE', "Replace", "Replace existing vertex colors with shadow values"),
            ('DARKEN', "Darken Only", "Keep the darker value between existing color and shadow"),
        ],
        default='MULTIPLY'
    )
    light_mode: bpy.props.EnumProperty(
        name="Light Source",
        description="Source of direct lighting direction",
        items=[
            ('MANUAL', "Manual Angles", "Set azimuth and elevation angles manually"),
            ('SUN_OBJECT', "Light Object", "Use rotation from a scene Light/Sun object"),
        ],
        default='MANUAL'
    )
    light_object: bpy.props.PointerProperty(
        name="Light",
        type=bpy.types.Object,
        description="Select Sun or Light object to take direction from"
    )
    light_azimuth: bpy.props.FloatProperty(
        name="Azimuth",
        description="Horizontal angle of sun (0-360 degrees)",
        default=45.0,
        min=0.0,
        max=360.0
    )
    light_elevation: bpy.props.FloatProperty(
        name="Elevation",
        description="Vertical elevation angle of sun (0-90 degrees)",
        default=60.0,
        min=0.0,
        max=90.0
    )
    cast_direct_shadows: bpy.props.BoolProperty(
        name="Cast Ray Shadows",
        description="Cast rays to detect geometry blocking the light (overhangs, walls, props)",
        default=True
    )
    ray_max_distance: bpy.props.FloatProperty(
        name="Ray Max Distance",
        description="Maximum distance to search for shadow blockers",
        default=500.0,
        min=0.1
    )
    shadow_bias: bpy.props.FloatProperty(
        name="Shadow Bias",
        description="Offset along normal to prevent self-shadow acne",
        default=0.005,
        min=0.0001,
        max=0.5
    )
    enable_terminator: bpy.props.BoolProperty(
        name="Terminator Shading",
        description="Darken surfaces pointing away from the light source",
        default=True
    )
    terminator_type: bpy.props.EnumProperty(
        name="Falloff",
        description="Terminator shading curve",
        items=[
            ('LAMBERT', "Lambert (Hard)", "Classic sharp cosine cutoff"),
            ('HALF_LAMBERT', "Half-Lambert (Soft)", "Smoother wrap-around lighting"),
            ('SMOOTH', "Smooth Step", "Gentle S-curve transition"),
        ],
        default='HALF_LAMBERT'
    )
    shadow_strength: bpy.props.FloatProperty(
        name="Shadow Darkness",
        description="Strength of the shadow darkening (1.0 = pitch dark, 0.0 = no shadow)",
        default=0.75,
        min=0.0,
        max=1.0
    )
    shadow_color: bpy.props.FloatVectorProperty(
        name="Shadow Tint",
        subtype='COLOR',
        description="Color tint for darkened shadow areas",
        default=(0.1, 0.12, 0.2),
        min=0.0,
        max=1.0
    )
    lit_color: bpy.props.FloatVectorProperty(
        name="Lit Tint",
        subtype='COLOR',
        description="Color tint for unshadowed areas",
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0
    )
    enable_ao: bpy.props.BoolProperty(
        name="Ambient Occlusion (Contact)",
        description="Darken crevices and contact points under overhangs or wheels",
        default=True
    )
    ao_distance: bpy.props.FloatProperty(
        name="AO Radius",
        description="Maximum search radius for contact occlusion",
        default=1.5,
        min=0.01,
        max=50.0
    )
    ao_sample_count: bpy.props.IntProperty(
        name="AO Samples",
        description="Number of hemispherical rays per vertex (higher = smoother, slower)",
        default=8,
        min=2,
        max=32
    )
    ao_strength: bpy.props.FloatProperty(
        name="AO Strength",
        description="Intensity of ambient occlusion darkening",
        default=0.6,
        min=0.0,
        max=1.0
    )


class VSP_PT_MainPanel(bpy.types.Panel):
    """Panel in 3D Viewport Sidebar (Vertex Shadow tab)"""
    bl_label = "Vertex Shadow Painter"
    bl_idname = "VSP_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Vertex Shadow'

    def draw(self, context):
        draw_vsp_ui(self.layout, context)


class VSP_PT_ToolPanel(bpy.types.Panel):
    """Panel in 3D Viewport Sidebar (Tool tab in Vertex Paint mode)"""
    bl_label = "Vertex Shadow Painter"
    bl_idname = "VSP_PT_tool_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tool'

    @classmethod
    def poll(cls, context):
        return context.mode in {'PAINT_VERTEX', 'OBJECT'}

    def draw(self, context):
        draw_vsp_ui(self.layout, context)


class VSP_PT_DataPanel(bpy.types.Panel):
    """Panel in Properties Editor > Object Data (Mesh tab)"""
    bl_label = "Vertex Shadow Painter"
    bl_idname = "VSP_PT_data_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'data'

    @classmethod
    def poll(cls, context):
        return context.object and context.object.type == 'MESH'

    def draw(self, context):
        draw_vsp_ui(self.layout, context)


def draw_vsp_ui(layout, context):
    props = context.scene.vsp_props

    # Big Prominent Action Button at the very top
    row = layout.row()
    row.scale_y = 1.8
    row.operator("paint.vsp_bake_shadows", icon='VPAINT_HLT', text="Bake Vertex Shadows Now")

    # Target Settings
    box = layout.box()
    box.label(text="Target Color Attribute", icon='GROUP_VCOL')
    box.prop(props, "layer_name")
    box.prop(props, "layer_domain")
    box.prop(props, "blend_mode")

    # Light Direction
    box = layout.box()
    box.label(text="Light Source", icon='LIGHT_SUN')
    box.prop(props, "light_mode", expand=True)
    if props.light_mode == 'SUN_OBJECT':
        box.prop(props, "light_object")
    else:
        row_ang = box.row()
        row_ang.prop(props, "light_azimuth")
        row_ang.prop(props, "light_elevation")

    # Direct Shadows & Terminator
    box = layout.box()
    box.label(text="Direct Shadow & Shading", icon='SHADING_RENDERED')
    box.prop(props, "cast_direct_shadows")
    if props.cast_direct_shadows:
        box.prop(props, "shadow_bias")
        box.prop(props, "ray_max_distance")

    box.prop(props, "enable_terminator")
    if props.enable_terminator:
        box.prop(props, "terminator_type")

    # Color & Darkness
    box = layout.box()
    box.label(text="Color & Darkness", icon='COLOR')
    row_c = box.row()
    row_c.prop(props, "shadow_color")
    row_c.prop(props, "lit_color")
    box.prop(props, "shadow_strength", slider=True)

    # Ambient Occlusion
    box = layout.box()
    box.label(text="Ambient Occlusion / Crevices", icon='PROP_CON')
    box.prop(props, "enable_ao")
    if props.enable_ao:
        box.prop(props, "ao_distance")
        box.prop(props, "ao_sample_count")
        box.prop(props, "ao_strength", slider=True)

    # Action Button also at the bottom
    layout.separator()
    row = layout.row()
    row.scale_y = 1.6
    row.operator("paint.vsp_bake_shadows", icon='VPAINT_HLT', text="Bake Vertex Shadows")


def menu_func_object(self, context):
    self.layout.separator()
    self.layout.operator("paint.vsp_bake_shadows", icon='VPAINT_HLT', text="Bake Vertex Shadows")


def menu_func_paint(self, context):
    self.layout.separator()
    self.layout.operator("paint.vsp_bake_shadows", icon='VPAINT_HLT', text="Bake Vertex Shadows")


classes = (
    VSP_Properties,
    VSP_OT_BakeShadows,
    VSP_PT_MainPanel,
    VSP_PT_ToolPanel,
    VSP_PT_DataPanel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.vsp_props = bpy.props.PointerProperty(type=VSP_Properties)
    bpy.types.VIEW3D_MT_object.append(menu_func_object)
    if hasattr(bpy.types, "VIEW3D_MT_paint_vertex"):
        bpy.types.VIEW3D_MT_paint_vertex.append(menu_func_paint)

def unregister():
    if hasattr(bpy.types, "VIEW3D_MT_paint_vertex"):
        bpy.types.VIEW3D_MT_paint_vertex.remove(menu_func_paint)
    bpy.types.VIEW3D_MT_object.remove(menu_func_object)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.vsp_props

if __name__ == "__main__":
    register()
