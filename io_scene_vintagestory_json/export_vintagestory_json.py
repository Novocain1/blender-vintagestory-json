import bpy
from bpy import context
from mathutils import Vector, Euler, Quaternion, Matrix
import math
import numpy as np
from math import inf
import posixpath # need "/" separator
import os
import json
from . import animation

import importlib
importlib.reload(animation)

# convert deg to rad
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi

# direction names for minecraft cube face UVs
DIRECTIONS = np.array([
    "north",
    "east",
    "west",
    "south",
    "up",
    "down",
])

# normals for minecraft directions in BLENDER world space
# e.g. blender (-1, 0, 0) is minecraft north (0, 0, -1)
# shape (f,n) = (6,3)
#   f = 6: number of cuboid faces to test
#   v = 3: vertex coordinates (x,y,z)
DIRECTION_NORMALS = np.array([
    [-1.,  0.,  0.],
    [ 0.,  1.,  0.],
    [ 0., -1.,  0.],
    [ 1.,  0.,  0.],
    [ 0.,  0.,  1.],
    [ 0.,  0., -1.],
])
# DIRECTION_NORMALS = np.tile(DIRECTION_NORMALS[np.newaxis,...], (6,1,1))

# blender counterclockwise uv -> minecraft uv rotation lookup table
# (these values experimentally determined)
# access using [uv_loop_start_index][vert_loop_start_index]
COUNTERCLOCKWISE_UV_ROTATION_LOOKUP = [
    [0, 270, 180, 90],
    [90, 0, 270, 180],
    [180, 90, 0, 270],
    [270, 180, 90, 0],
]

# blender clockwise uv -> minecraft uv rotation lookup table
# (these values experimentally determined)
# access using [uv_loop_start_index][vert_loop_start_index]
# Note: minecraft uv must also be x-flipped
CLOCKWISE_UV_ROTATION_LOOKUP = [
    [90, 0, 270, 180],
    [0, 270, 180, 90],
    [270, 180, 90, 0],
    [180, 90, 0, 270],
]

# maps a PoseBone rotation mode name to the proper
# action Fcurve property type
ROTATION_MODE_TO_FCURVE_PROPERTY = {
    "QUATERNION": "rotation_quaternion",
    "XYZ": "rotation_euler",
    "XZY": "rotation_euler",
    "YXZ": "rotation_euler",
    "YZX": "rotation_euler",
    "ZXY": "rotation_euler",
    "ZYX": "rotation_euler",
    "AXIS_ANGLE": "rotation_euler", # ??? TODO: handle properly...
}

def filter_root_objects(objects):
    """Get root objects (objects without parents) in
    Blender scene"""
    root_objects = []
    for obj in objects:
        if obj.parent is None:
            root_objects.append(obj)
    return root_objects


def sign(x):
    """Return sign of value as 1 or -1"""
    if x >= 0:
        return 1
    else:
        return -1


def to_vintagestory_axis(ax):
    """Convert blender space to VS space:
    X -> Z
    Y -> X
    Z -> Y
    """
    if "X" in ax:
        return "Z"
    elif "Y" in ax:
        return "X"
    elif "Z" in ax:
        return "Y"


def to_y_up(arr):
    """Convert blender space to VS space:
    X -> Z
    Y -> X
    Z -> Y
    """
    return np.array([arr[1], arr[2], arr[0]])


def to_vintagestory_rotation(euler):
    """Convert blender space rotation to VS space:
    VS space XYZ euler is blender space XZY euler,
    so convert euler order, then rename axes
        X -> Z
        Y -> X
        Z -> Y
    Inputs:
    - euler: Blender euler rotation
    """
    r = euler.to_quaternion().to_euler("XZY")
    return np.array([
        r.y * RAD_TO_DEG,
        r.z * RAD_TO_DEG,
        r.x * RAD_TO_DEG,
    ])


def clamp_rotation(r):
    """Clamp a euler angle rotation in numpy array format
    [rx, ry, rz] to within bounds [-360, 360]
    """
    while r[0] > 360.0:
        r[0] -= 360.0
    while r[0] < -360.0:
        r[0] += 360.0
    
    while r[1] > 360.0:
        r[1] -= 360.0
    while r[1] < -360.0:
        r[1] += 360.0
    
    while r[2] > 360.0:
        r[2] -= 360.0
    while r[2] < -360.0:
        r[2] += 360.0
    
    return r


def get_reduced_rotation(rotation):
    """Split rotation into all 90 deg rotations
    and the residual. Return a rotation matrix for all 90 deg rotations
    or None if not required.
    """
    residual_rotation = rotation.copy()
    
    # number of 90 degree steps
    xsteps = 0
    ysteps = 0
    zsteps = 0
    
    # get sign of euler angles
    rot_x_sign = sign(rotation.x)
    rot_y_sign = sign(rotation.y)
    rot_z_sign = sign(rotation.z)
    
    eps = 1e-6 # angle error range for floating point precision issues
    
    # included sign() in loop condition to prevent angle
    # overflowing to other polarity
    while abs(residual_rotation.x) > math.pi/2 - eps and sign(residual_rotation.x) == rot_x_sign:
        angle = rot_x_sign * math.pi/2
        residual_rotation.x -= angle
        xsteps += rot_x_sign
    while abs(residual_rotation.y) > math.pi/2 - eps and sign(residual_rotation.y) == rot_y_sign:
        angle = rot_y_sign * math.pi/2
        residual_rotation.y -= angle
        ysteps += rot_y_sign
    while abs(residual_rotation.z) > math.pi/4 - eps and sign(residual_rotation.z) == rot_z_sign:
        angle = rot_z_sign * math.pi/2
        residual_rotation.z -= angle
        zsteps += rot_z_sign
    
    if xsteps != 0 or ysteps != 0 or zsteps != 0:
        rotation_90deg = Euler((xsteps * math.pi/2, ysteps * math.pi/2, zsteps * math.pi/2), "XYZ")
    else:
        rotation_90deg = None
    
    return residual_rotation, rotation_90deg


class TextureInfo():
    """Description of a texture, gives image source path and size
    """
    def __init__(self, path, size):
        self.path = path
        self.size = size


def get_material_color(mat):
    """Get material color as tuple (r, g, b, a). Return None if no
    material node has color property.
    Inputs:
    - mat: Material
    Returns:
    - color tuple (r, g, b,a ) if a basic color,
      "texture_path" string if a texture,
      or None if no color/path
    """
    # get first node with valid color
    if mat.node_tree is not None:
        for n in mat.node_tree.nodes:
            # principled BSDF
            if "Base Color" in n.inputs:
                node_color = n.inputs["Base Color"]
                # check if its a texture path
                for link in node_color.links:
                    from_node = link.from_node
                    if isinstance(from_node, bpy.types.ShaderNodeTexImage):
                        if from_node.image is not None and from_node.image.filepath != "":
                            img = from_node.image
                            img_size = [img.size[0], img.size[1]]
                            return TextureInfo(img.filepath, img_size)
                # else, export color tuple
                color = node_color.default_value
                color = (color[0], color[1], color[2], color[3])
                return color
            # most other materials with color
            elif "Color" in n.inputs:
                color = n.inputs["Color"].default_value
                color = (color[0], color[1], color[2], color[3])
                return color
    
    return None


def get_object_color(obj, material_index, default_color = (0.0, 0.0, 0.0, 1.0)):
    """Get obj material color in index as either 
    - tuple (r, g, b, a) if using a default color input
    - texture file name string "path" if using a texture input
    """
    if material_index < len(obj.material_slots):
        slot = obj.material_slots[material_index]
        material = slot.material
        if material is not None:
            color = get_material_color(material)
            if color is not None:
                return color
    
    return default_color


def loop_is_clockwise(coords):
    """Detect if loop of 2d coordinates is clockwise or counterclockwise.
    Inputs:
    - coords: List of 2d array indexed coords, [p0, p1, p2, ... pN]
              where each is array indexed as p[0] = p0.x, p[1] = p0.y
    Returns:
    - True if clockwise, False if counterclockwise
    """
    num_coords = len(coords)
    area = 0
    
    # use polygon winding area to detect if loop is clockwise or counterclockwise
    for i in range(num_coords):
        # next index
        k = i + 1 if i < num_coords - 1 else 0
        area += (coords[k][0] - coords[i][0]) * (coords[k][1] + coords[i][1])
    
    # clockwise if area positive
    return area > 0


def create_color_texture(
    colors,
    min_size = 16,
):
    """Create a packed square texture from list of input colors. Each color
    is a distinct RGB tuple given a 3x3 pixel square in the texture. These
    must be 3x3 pixels so that there is no uv bleeding near the face edges.
    Also includes a tile for a default color for faces with no material.
    This is the next unfilled 3x3 tile.

    Inputs:
    - colors: Iterable of colors. Each color should be indexable like an rgb
              tuple c = (r, g, b), just so that r = c[0], b = c[1], g = c[2].
    - min_size: Minimum size of texture (must be power 2^n). By default
                16 because Minecraft needs min sized 16 textures for 4 mipmap levels.'
    
    Returns:
    - tex_pixels: Flattened array of texture pixels.
    - tex_size: Size of image texture.
    - color_tex_uv_map: Dict map from rgb tuple color to minecraft format uv coords
                        (r, g, b) -> (xmin, ymin, xmax, ymax)
    - default_color_uv: Default uv coords for unmapped materials (xmin, ymin, xmax, ymax).
    """
    # blender interprets (r,g,b,a) in sRGB space
    def linear_to_sRGB(v):
        if v < 0.0031308:
            return v * 12.92
        else:
            return 1.055 * (v ** (1/2.4)) - 0.055
    
    # fit textures into closest (2^n,2^n) sized texture
    # each color takes a (3,3) pixel chunk to avoid color
    # bleeding at UV edges seams
    # -> get smallest n to fit all colors, add +1 for a default color tile
    color_grid_size = math.ceil(math.sqrt(len(colors) + 1)) # colors on each axis
    tex_size = max(min_size, 2 ** math.ceil(math.log2(3 * color_grid_size))) # fit to (2^n, 2^n) image
    
    # composite colors into white RGBA grid
    tex_colors = np.ones((color_grid_size, color_grid_size, 4))
    color_tex_uv_map = {}
    for i, c in enumerate(colors):
        # convert color to sRGB
        c_srgb = (linear_to_sRGB(c[0]), linear_to_sRGB(c[1]), linear_to_sRGB(c[2]), c[3])

        tex_colors[i // color_grid_size, i % color_grid_size, :] = c_srgb
        
        # uvs: [x1, y1, x2, y2], each value from [0, 16] as proportion of image
        # map each color to a uv
        x1 = ( 3*(i % color_grid_size) + 1 ) / tex_size * 16
        x2 = ( 3*(i % color_grid_size) + 2 ) / tex_size * 16
        y1 = ( 3*(i // color_grid_size) + 1 ) / tex_size * 16
        y2 = ( 3*(i // color_grid_size) + 2 ) / tex_size * 16
        color_tex_uv_map[c] = [x1, y1, x2, y2]
    
    # default color uv coord (last coord + 1)
    idx = len(colors)
    default_color_uv = [
        ( 3*(idx % color_grid_size) + 1 ) / tex_size * 16,
        ( 3*(idx // color_grid_size) + 1 ) / tex_size * 16,
        ( 3*(idx % color_grid_size) + 2 ) / tex_size * 16,
        ( 3*(idx // color_grid_size) + 2 ) / tex_size * 16
    ]

    # triple colors into 3x3 pixel chunks
    tex_colors = np.repeat(tex_colors, 3, axis=0)
    tex_colors = np.repeat(tex_colors, 3, axis=1)
    tex_colors = np.flip(tex_colors, axis=0)

    # pixels as flattened array (for blender Image api)
    tex_pixels = np.ones((tex_size, tex_size, 4))
    tex_pixels[-tex_colors.shape[0]:, 0:tex_colors.shape[1], :] = tex_colors
    tex_pixels = tex_pixels.flatten("C")

    return tex_pixels, tex_size, color_tex_uv_map, default_color_uv


def generate_element(
    obj,                           # current object
    parent=None,                   # parent Blender object
    armature=None,                 # Blender Armature object (NOT Armature data)
    bone_hierarchy=None,           # map of armature bones => children mesh objects
    groups=None,                   # running dict of collections
    model_colors=None,             # running dict of all model colors
    model_textures=None,           # running dict of all model textures
    parent_matrix_world=None,      # parent matrix world transform 
    parent_cube_origin=None,       # parent cube "from" origin (coords in VintageStory space)
    parent_rotation_origin=None,   # parent object rotation origin (coords in VintageStory space)
    parent_rotation_90deg=None,    # parent 90 degree rotation matrix
    export_uvs=True,               # export uvs
    export_generated_texture=True,
):
    """Recursive function to generate output element from
    Blender object

    See diagram from importer for location transformation.

    VintageStory => Blender space:
        child_blender_origin = parent_cube_origin - parent_rotation_origin + child_rotation_origin
        from_blender_local = from - child_rotation_origin
        to_blender_local = to - child_rotation_origin

    Blender space => VS Space:
        child_rotation_origin = child_blender_origin - parent_cube_origin + parent_rotation_origin
        from = from_blender_local + child_rotation_origin
        to = to_blender_local + child_rotation_origin
    """

    mesh = obj.data
    if not isinstance(mesh, bpy.types.Mesh):
        return None
    
    obj_name = obj.name # may be overwritten if part of armature

    # count number of vertices, ignore if not cuboid
    num_vertices = len(mesh.vertices)
    if num_vertices != 8:
        return None

    # get local mesh coordinates
    v_local = np.zeros((3, 8))
    for i, v in enumerate(mesh.vertices):
        v_local[0:3,i] = v.co

    """
    object blender origin and rotation
    -> if this is part of an armature, must get relative
    to parent bone
    """
    origin = obj.location.copy()
    bone_location = None
    bone_origin = None
    obj_rotation = obj.rotation_euler.copy()
    origin_bone_offset = np.array([0., 0., 0.])
    matrix_world = obj.matrix_world.copy()

    if armature is not None and obj.parent_bone != "":
        bone_name = obj.parent_bone
        if bone_name in armature.data.bones and bone_name in bone_hierarchy and bone_hierarchy[bone_name].main.name == obj.name:
            # origin_bone_offset = obj.location - origin
            bone = armature.data.bones[bone_name]
            # bone_location = relative location of bone from its parent bone
            if bone.parent is not None:
                bone_location = bone.head_local - bone.parent.head_local
            else:
                bone_location = bone.head_local
            
            bone_origin = bone.head_local
            origin_bone_offset = origin - bone.head_local
            matrix_world.translation = bone.head_local

    # more robust but higher performance cost, just get relative
    # location/rotation from world matrices, required for complex
    # parent hierarchies with armature bones + object-object parenting
    # TODO: global flag for mesh with an armature so this is used instead
    # of just obj.location and obj.rotation_euler
    if parent_matrix_world is not None:
        mat_local = parent_matrix_world.inverted_safe() @ matrix_world
        origin, quat, _ = mat_local.decompose()
        obj_rotation = quat.to_euler("XYZ")

        # adjustment for vertices
        if bone_origin is not None:
            origin_bone_offset = obj_rotation.to_matrix().to_4x4().inverted_safe() @ origin_bone_offset
    # using bone origin instead of parent origin offset
    elif bone_location is not None:
        origin = bone_location
    
    # ================================
    # apply parent 90 deg rotations
    # ================================
    if parent_rotation_90deg is not None:
        ax_angle, theta = obj_rotation.to_quaternion().to_axis_angle()
        transformed_ax_angle = parent_rotation_90deg @ ax_angle
        obj_rotation = Quaternion(transformed_ax_angle, theta).to_euler("XYZ")
        v_local = parent_rotation_90deg @ v_local
        origin = parent_rotation_90deg @ origin
    
    # ================================
    # constrain rotation to [-90, 90] by applying all further
    # 90 deg rotations directly to vertices
    # ================================
    residual_rotation, rotation_90deg = get_reduced_rotation(obj_rotation)

    if rotation_90deg is not None:
        mat_rotate_90deg = np.array(rotation_90deg.to_matrix())
    
        # rotate axis of residual rotation
        # TODO: need to handle with armature, right now messes up rotation
        # relative to bone
        if armature is None:
            ax_angle, theta = residual_rotation.to_quaternion().to_axis_angle()
            transformed_ax_angle = mat_rotate_90deg @ ax_angle
            obj_rotation = Quaternion(transformed_ax_angle, theta).to_euler("XYZ")

            # rotate mesh vertices
            v_local = mat_rotate_90deg @ v_local
    else:
        mat_rotate_90deg = None
    
    # create output coords, rotation
    # get min/max for to/from points
    v_min = np.amin(v_local, axis=1)
    v_max = np.amax(v_local, axis=1)

    # change axis to vintage story y-up axis
    v_min = to_y_up(v_min)
    v_max = to_y_up(v_max)
    origin = to_y_up(origin)
    origin_bone_offset = to_y_up(origin_bone_offset)
    rotation = to_vintagestory_rotation(obj_rotation)
    
    # translate to vintage story coord space
    rotation_origin = origin - parent_cube_origin + parent_rotation_origin
    v_from = v_min + rotation_origin + origin_bone_offset
    v_to = v_max + rotation_origin + origin_bone_offset
    cube_origin = v_from
    
    # ================================
    # texture/uv generation
    # 
    # NOTE: BLENDER VS MINECRAFT/VINTAGE STORY UV AXIS
    # - blender: uvs origin is bottom-left (0,0) to top-right (1, 1)
    # - minecraft/vs: uvs origin is top-left (0,0) to bottom-right (16, 16)
    # minecraft uvs: [x1, y1, x2, y2], each value from [0, 16] as proportion of image
    # as well as 0, 90, 180, 270 degree uv rotation

    # uv loop to export depends on:
    # - clockwise/counterclockwise order
    # - uv starting coordinate (determines rotation) relative to face
    #   vertex loop starting coordinate
    # 
    # Assume "natural" index order of face vertices and uvs without
    # any rotations in local mesh space is counterclockwise loop:
    #   3___2      ^ +y
    #   |   |      |
    #   |___|      ---> +x
    #   0   1
    # 
    # uv, vertex starting coordinate is based on this loop.
    # Use the uv rotation lookup tables constants to determine rotation.
    # ================================

    # initialize faces
    faces = {
        "north": {"texture": "#0", "uv": [0, 0, 4, 4], "autoUv": False},
        "east": {"texture": "#0", "uv": [0, 0, 4, 4], "autoUv": False},
        "south": {"texture": "#0", "uv": [0, 0, 4, 4], "autoUv": False},
        "west": {"texture": "#0", "uv": [0, 0, 4, 4], "autoUv": False},
        "up": {"texture": "#0", "uv": [0, 0, 4, 4], "autoUv": False},
        "down": {"texture": "#0", "uv": [0, 0, 4, 4], "autoUv": False},
    }
    
    uv_layer = mesh.uv_layers.active.data

    for i, face in enumerate(mesh.polygons):
        if i > 5: # should be 6 faces only
            print(f"WARNING: {obj} has >6 faces")
            break

        # stack + reshape to (6,3)
        # face_normal = np.array(face.normal)
        # face_normal_stacked = np.transpose(face_normal[..., np.newaxis], (1,0))
        face_normal = face.normal
        if parent_rotation_90deg is not None:
            face_normal = parent_rotation_90deg @ face_normal
        if mat_rotate_90deg is not None:
            face_normal = mat_rotate_90deg @ face_normal
        face_normal = np.array(face_normal)
        face_normal_stacked = np.transpose(face_normal[..., np.newaxis], (1,0))
        face_normal_stacked = np.tile(face_normal_stacked, (6,1))

        # get face direction string
        face_direction_index = np.argmax(np.sum(face_normal_stacked * DIRECTION_NORMALS, axis=1), axis=0)
        d = DIRECTIONS[face_direction_index]
        
        face_texture = get_object_color(obj, face.material_index)
        
        # solid color tuple
        if isinstance(face_texture, tuple) and export_generated_texture:
            faces[d] = face_texture # replace face with color
            if model_colors is not None:
                model_colors.add(face_texture)
        # texture
        elif isinstance(face_texture, TextureInfo):
            faces[d]["texture"] = face_texture.path
            model_textures[face_texture.path] = face_texture

            tex_width = face_texture.size[0]
            tex_height = face_texture.size[1]

            if export_uvs:
                # uv loop
                loop_start = face.loop_start
                face_uv_0 = uv_layer[loop_start].uv
                face_uv_1 = uv_layer[loop_start+1].uv
                face_uv_2 = uv_layer[loop_start+2].uv
                face_uv_3 = uv_layer[loop_start+3].uv

                uv_min_x = min(face_uv_0[0], face_uv_2[0])
                uv_max_x = max(face_uv_0[0], face_uv_2[0])
                uv_min_y = min(face_uv_0[1], face_uv_2[1])
                uv_max_y = max(face_uv_0[1], face_uv_2[1])

                uv_clockwise = loop_is_clockwise([face_uv_0, face_uv_1, face_uv_2, face_uv_3])

                # vertices loops
                # project 3d vertex loop onto 2d loop based on face normal,
                # minecraft uv mapping starting corner experimentally determined
                verts = [ v_local[:,v] for v in face.vertices ]
                
                if face_normal[0] > 0.5: # normal = (1, 0, 0)
                    verts = [ (v[1], v[2]) for v in verts ]
                elif face_normal[0] < -0.5: # normal = (-1, 0, 0)
                    verts = [ (-v[1], v[2]) for v in verts ]
                elif face_normal[1] > 0.5: # normal = (0, 1, 0)
                    verts = [ (-v[0], v[2]) for v in verts ]
                elif face_normal[1] < -0.5: # normal = (0, -1, 0)
                    verts = [ (v[0], v[2]) for v in verts ]
                elif face_normal[2] > 0.5: # normal = (0, 0, 1)
                    verts = [ (v[1], -v [0]) for v in verts ]
                elif face_normal[2] < -0.5: # normal = (0, 0, -1)
                    verts = [ (v[1], v[0]) for v in verts ]
                
                vert_min_x = min(verts[0][0], verts[2][0])
                vert_max_x = max(verts[0][0], verts[2][0])
                vert_min_y = min(verts[0][1], verts[2][1])
                vert_max_y = max(verts[0][1], verts[2][1])

                vert_clockwise = loop_is_clockwise(verts)
                
                # get uv, vert loop starting corner index 0..3 in face loop

                # uv start corner index
                uv_start_x = face_uv_0[0]
                uv_start_y = face_uv_0[1]
                if uv_start_y < uv_max_y:
                    # start coord 0
                    if uv_start_x < uv_max_x:
                        uv_loop_start_index = 0
                    # start coord 1
                    else:
                        uv_loop_start_index = 1
                else:
                    # start coord 2
                    if uv_start_x > uv_min_x:
                        uv_loop_start_index = 2
                    # start coord 3
                    else:
                        uv_loop_start_index = 3
                
                # vert start corner index
                vert_start_x = verts[0][0]
                vert_start_y = verts[0][1]
                if vert_start_y < vert_max_y:
                    # start coord 0
                    if vert_start_x < vert_max_x:
                        vert_loop_start_index = 0
                    # start coord 1
                    else:
                        vert_loop_start_index = 1
                else:
                    # start coord 2
                    if vert_start_x > vert_min_x:
                        vert_loop_start_index = 2
                    # start coord 3
                    else:
                        vert_loop_start_index = 3

                # set uv flip and rotation based on
                # 1. clockwise vs counterclockwise loop
                # 2. relative starting corner difference between vertex loop and uv loop
                # NOTE: if face normals correct, vertices should always be counterclockwise...
                face_uvs = np.zeros((4,))

                if uv_clockwise == False and vert_clockwise == False:
                    face_uvs[0] = uv_min_x
                    face_uvs[1] = uv_max_y
                    face_uvs[2] = uv_max_x
                    face_uvs[3] = uv_min_y
                    face_uv_rotation = COUNTERCLOCKWISE_UV_ROTATION_LOOKUP[uv_loop_start_index][vert_loop_start_index]
                elif uv_clockwise == True and vert_clockwise == False:
                    # invert x face uvs
                    face_uvs[0] = uv_max_x
                    face_uvs[1] = uv_max_y
                    face_uvs[2] = uv_min_x
                    face_uvs[3] = uv_min_y
                    face_uv_rotation = CLOCKWISE_UV_ROTATION_LOOKUP[uv_loop_start_index][vert_loop_start_index]
                elif uv_clockwise == False and vert_clockwise == True:
                    # invert y face uvs, case should not happen
                    face_uvs[0] = uv_max_x
                    face_uvs[1] = uv_max_y
                    face_uvs[2] = uv_min_x
                    face_uvs[3] = uv_min_y
                    face_uv_rotation = CLOCKWISE_UV_ROTATION_LOOKUP[uv_loop_start_index][vert_loop_start_index]
                else: # uv_clockwise == True and vert_clockwise == True:
                    # case should not happen
                    face_uvs[0] = uv_min_x
                    face_uvs[1] = uv_max_y
                    face_uvs[2] = uv_max_x
                    face_uvs[3] = uv_min_y
                    face_uv_rotation = COUNTERCLOCKWISE_UV_ROTATION_LOOKUP[uv_loop_start_index][vert_loop_start_index]
                
                xmin = face_uvs[0] * tex_width
                ymin = (1.0 - face_uvs[1]) * tex_height
                xmax = face_uvs[2] * tex_width
                ymax = (1.0 - face_uvs[3]) * tex_height
                faces[d]["uv"] = [ xmin, ymin, xmax, ymax ]
                
                if face_uv_rotation != 0 and face_uv_rotation != 360:
                    faces[d]["rotation"] = face_uv_rotation if face_uv_rotation >= 0 else 360 + face_uv_rotation
    
    # ================================
    # build children
    # ================================
    children = []
    attachpoints = []

    # combine 90 deg rotation matrix for child
    if mat_rotate_90deg is not None:
        if parent_rotation_90deg is not None:
            mat_rotate_90deg = mat_rotate_90deg @ parent_rotation_90deg
    else:
        mat_rotate_90deg = parent_rotation_90deg
    
    # parse direct children objects normally
    for child in obj.children:
        # attach point empty marker
        if child.type == "EMPTY":
            attachpoint_element = generate_attach_point(
                child,
                parent=obj,
                armature=armature,
                parent_cube_origin=cube_origin,
                parent_rotation_origin=rotation_origin,
                parent_rotation_90deg=mat_rotate_90deg,
            )
            if attachpoint_element is not None:
                attachpoints.append(attachpoint_element)
        else: # assume normal mesh
            child_element = generate_element(
                child,
                parent=obj,
                armature=None,
                bone_hierarchy=None,
                groups=groups,
                model_colors=model_colors,
                model_textures=model_textures,
                parent_matrix_world=matrix_world,
                parent_cube_origin=cube_origin,
                parent_rotation_origin=rotation_origin,
                parent_rotation_90deg=mat_rotate_90deg,
                export_uvs=export_uvs,
            )
            if child_element is not None:
                children.append(child_element)

    # use parent bone children if this is part of an armature
    if bone_hierarchy is not None:
        bone_obj_children = []
        parent_bone_name = obj.parent_bone
        if parent_bone_name != "" and parent_bone_name in bone_hierarchy and parent_bone_name in armature.data.bones:
            # if this is main bone, parent other objects to this
            if bone_hierarchy[parent_bone_name].main.name == obj.name:
                # rename this object to the bone name
                obj_name = parent_bone_name

                # parent other objects in same bone to this object
                if len(bone_hierarchy[parent_bone_name].children) > 1:
                    bone_obj_children.extend(bone_hierarchy[parent_bone_name].children[1:])
                
                # parent children main objects to this
                parent_bone = armature.data.bones[parent_bone_name]
                for child_bone in parent_bone.children:
                    child_bone_name = child_bone.name
                    if child_bone_name in bone_hierarchy:
                        bone_obj_children.append(bone_hierarchy[child_bone_name].main)
    
        for child in bone_obj_children:
            child_element = generate_element(
                child,
                parent=obj,
                armature=armature,
                bone_hierarchy=bone_hierarchy,
                groups=groups,
                model_colors=model_colors,
                model_textures=model_textures,
                parent_matrix_world=matrix_world,
                parent_cube_origin=cube_origin,
                parent_rotation_origin=rotation_origin,
                parent_rotation_90deg=mat_rotate_90deg,
                export_uvs=export_uvs,
            )
            if child_element is not None:
                children.append(child_element)

    # ================================
    # build element
    # ================================
    new_element = {
        "name": obj_name,
        "from": v_from,
        "to": v_to,
        "rotationOrigin": rotation_origin,
    }

    # add rotations
    if rotation[0] != 0.0:
        new_element["rotationX"] = rotation[0]
    if rotation[1] != 0.0:
        new_element["rotationY"] = rotation[1]
    if rotation[2] != 0.0:
        new_element["rotationZ"] = rotation[2]

    # add collection link
    collection = obj.users_collection[0]
    if collection is not None:
        new_element["group"] = collection.name

    # add faces
    new_element["faces"] = faces

    # add children
    new_element["children"] = children

    # add attachpoints if they exist
    if len(attachpoints) > 0:
        new_element["attachmentpoints"] = attachpoints
    
    return new_element


def generate_attach_point(
    obj,                           # current object
    parent=None,                   # parent Blender object
    armature=None,                 # Blender Armature object (NOT Armature data)
    parent_cube_origin=None,       # parent cube "from" origin (coords in VintageStory space)
    parent_rotation_origin=None,   # parent object rotation origin (coords in VintageStory space)
    parent_rotation_90deg=None,    # parent 90 degree rotation matrix
):
    """Parse an attachment point
    """
    if not obj.name.startswith("attach_"):
        return None
    
    # get attachpoint name
    name = obj.name[7:]

    """
    object blender origin and rotation
    -> if this is part of an armature, must get relative
    to parent bone
    """
    origin = np.array(obj.location)
    obj_rotation = obj.rotation_euler

    if armature is not None and obj.parent_bone != "":
        bone_name = obj.parent_bone
        if bone_name in armature.data.bones:
            bone_matrix = armature.data.bones[bone_name].matrix_local
            # print(obj.name, "BONE MATRIX:", bone_matrix)
        mat_loc = parent.matrix_world.inverted_safe() @ obj.matrix_world
        origin, quat, _ = mat_loc.decompose()
        obj_rotation = quat.to_euler("XYZ")

    # ================================
    # apply parent 90 deg rotations
    # ================================
    if parent_rotation_90deg is not None:
        ax_angle, theta = obj_rotation.to_quaternion().to_axis_angle()
        transformed_ax_angle = parent_rotation_90deg @ ax_angle
        obj_rotation = Quaternion(transformed_ax_angle, theta).to_euler("XYZ")
        origin = parent_rotation_90deg @ origin
    
    # ================================
    # constrain rotation to [-90, 90] by applying all further
    # 90 deg rotations directly to vertices
    # ================================
    residual_rotation, rotation_90deg = get_reduced_rotation(obj_rotation)

    if rotation_90deg is not None:
        mat_rotate_90deg = np.array(rotation_90deg.to_matrix())
    
        # rotate axis of residual rotation
        ax_angle, theta = residual_rotation.to_quaternion().to_axis_angle()
        transformed_ax_angle = mat_rotate_90deg @ ax_angle
        obj_rotation = Quaternion(transformed_ax_angle, theta).to_euler("XYZ")
    else:
        mat_rotate_90deg = None

    # change axis to vintage story y-up axis
    origin = to_y_up(origin)
    rotation = to_vintagestory_rotation(obj_rotation)
    
    # translate to vintage story coord space
    rotation_origin = origin - parent_cube_origin + parent_rotation_origin

    return {
        "code": name,
        "posX": rotation_origin[0],
        "posY": rotation_origin[1],
        "posZ": rotation_origin[2],
        "rotationX": rotation[0],
        "rotationY": rotation[1],
        "rotationZ": rotation[2],
    }


class BoneNode():
    """Contain information on bone hierarchy: bones in armature and
    associated Blender object children of the bone. For exporting to
    Vintage Story, there are no bones, so one of these objects needs to
    act as the bone. The "main" object is the object that will be the
    bone after export.

    Keep main object in index 0 of children, so can easily get non-main
    children using children[1:]
    """
    def __init__(self):
        self.main = None   # main object for this bone
        self.children = [] # blender object children associated with this bone
                           # main object should always be index 0


def get_bone_hierarchy(armature):
    """Create map of armature bone name => BoneNode objects.

    "Main" bone determined by following rule in order:
    1. if a child object has same name as the bone, set as main
    2. else, use first object
    """
    bone_hierarchy = {}
    for obj in armature.children:
        if obj.parent_bone != "":
            if obj.parent_bone not in bone_hierarchy:
                bone_hierarchy[obj.parent_bone] = BoneNode()
            bone_hierarchy[obj.parent_bone].children.append(obj)
    
    for bone_name, node in bone_hierarchy.items():
        for i, obj in enumerate(node.children):
            if obj.name == bone_name:
                node.main = obj
                node.children[0], node.children[i] = node.children[i], node.children[0] # swap so main index 0
                break
        # use first object
        if node.main is None:
            node.main = node.children[0]
    
    return bone_hierarchy


def save_all_animations():
    """Save all animation actions in Blender file
    """
    animations = []

    if len(bpy.data.armatures) == 0:
        return animations
    
    # get armature, assume single armature
    armature = bpy.data.armatures[0]
    try:
        obj_armature = bpy.data.objects[armature.name]
    except Exception as err:
        print("Error finding armature:", err)
        return animations
    bones = obj_armature.pose.bones

    for action in bpy.data.actions:
        # get all actions
        fcurves = action.fcurves
        
        # skip empty actions
        if len(fcurves) == 0:
            continue
        
        # load keyframe data
        action_name = action.name
        animation_adapter = animation.AnimationAdapter(action, name=action_name, armature=armature)

        # sort fcurves by bone
        for fcu in fcurves:
            # read bone name in format: path.bones["name"].property
            data_path = fcu.data_path
            if not data_path.startswith("pose.bones"):
                continue
            
            # read bone name
            idx_bone_name_start = 12                    # [" index
            idx_bone_name_end = data_path.find("]", 12) # "] index
            bone_name = data_path[idx_bone_name_start:idx_bone_name_end-1]

            # skip if bone not found (TODO)
            # if bone_name not in bones:
            #     continue
            
            bone = bones[bone_name]
            rotation_mode = bone.rotation_mode

            # match data_path property to export name
            property_name = data_path[idx_bone_name_end+2:]

            if property_name != "location":
                # make sure rotation curve associated with proper rotation mode
                # e.g. bone with XYZ euler mode should only use "rotation_euler" fcurve
                #      since rotation_quaternion fcurves can still exist
                if ROTATION_MODE_TO_FCURVE_PROPERTY[rotation_mode] != property_name:
                    continue

            # add bone and fcurve to animation adapter
            animation_adapter.set_bone_rotation_mode(bone_name, ROTATION_MODE_TO_FCURVE_PROPERTY[rotation_mode])
            animation_adapter.add_fcurve(fcu, data_path, fcu.array_index)

        # convert from Blender bone format to Vintage story format
        keyframes = animation_adapter.create_vintage_story_keyframes()

        # action metadata
        quantity_frames = None
        on_activity_stopped = "Stop"
        on_animation_end = "Stop"

        # parse metadata from pose markers
        for marker in action.pose_markers:
            if marker.name.startswith("onActivityStopped_"):
                on_activity_stopped = marker.name[18:]
            elif marker.name.startswith("onAnimationEnd_"):
                quantity_frames = marker.frame + 1
                on_animation_end = marker.name[15:]
        
        # if quantity frames not set from marker metadata, set to last keyframe + 1 (starts at 0)
        if quantity_frames is None:
            if len(keyframes) > 0:
                quantity_frames = int(keyframes[-1]["frame"]) + 1
            else:
                quantity_frames = 0
        
        # create exported animation
        action_export = {
            "name": action_name,
            "code": action_name,
            "quantityframes": quantity_frames,
            "onActivityStopped": on_activity_stopped,
            "onAnimationEnd": on_animation_end,
            "keyframes": keyframes,
        }

        animations.append(action_export)

    return animations


def save_objects(
    filepath,
    objects,
    translate_origin=None,
    generate_texture=True,
    use_only_exported_object_colors=False,
    texture_folder="",
    color_texture_filename="",
    export_uvs=True,
    minify=False,
    decimal_precision=-1,
    export_armature=True,
    export_animations=True,
    **kwargs
):
    """Main exporter function. Parses Blender objects into VintageStory
    cuboid format, uvs, and handles texture read and generation.
    Will save .json file to output path.

    Inputs:
    - filepath: Output file path name.
    - object: Iterable collection of Blender objects
    - recenter_origin: Recenter model so that its center is at specified translate_origin
    - translate_origin: New origin to shift, None for no shift, [x, y, z] list in Blender coords
                        to apply shift
    - generate_texture: Generate texture from solid material colors. By default, creates
            a color texture from all materials in file (so all groups of
            objects can share the same texture file).
    - use_only_exported_object_colors:
            Generate texture colors from only exported objects instead of default using
            all file materials.
    - texture_folder: Output texture subpath, for typical "item/texture_name" the
            texture folder would be "item".
    - color_texture_filename: Name of exported color texture file.
    - export_uvs: Export object uvs.
    - minify: Minimize output file size (write into single line, remove spaces, ...)
    - decimal_precision: Number of digits after decimal to keep in numbers.
            Requires `minify = True`. Set to -1 to disable.
    - export_animations: Export bones and animation actions.
    """
    
    # debug
    print("ORIGIN SHIFT:", translate_origin)
    print("")

    # output json model stub
    model_json = {
        "textureWidth": 16,  # default, will be overridden
        "textureHeight": 16, # default, will be overridden
        "textures": {},
        "textureSizes": {},
    }

    # elements at top of hierarchy
    root_elements = []

    # object collections to be exported
    groups = {}
    
    # all material colors tuples from all object faces
    if use_only_exported_object_colors:
        model_colors = set()
    else:
        model_colors = None
    
    # all material texture paths and sizes:
    # tex_name => {
    #   "path": path,
    #   "size": [x, y],
    # }
    model_textures = {}
    
    # first pass: check if parsing an armature
    armature = None
    bone_hierarchy = None
    export_objects = objects

    if export_armature:
        for obj in objects:
            if isinstance(obj.data, bpy.types.Armature):
                armature = obj
                
                # reset all pose bones in armature to bind pose
                for bone in armature.pose.bones:
                    bone.matrix_basis = Matrix.Identity(4)
                bpy.context.view_layer.update() # force update
                
                bone_hierarchy = get_bone_hierarchy(armature)

                # do export using root bone children
                root_bones = filter_root_objects(armature.data.bones)
                export_objects = []
                for bone in root_bones:
                    export_objects.append(bone_hierarchy[bone.name].main)
                
                break
        
        # for debugging
        # print("EXPORT OBJECTS", export_objects)
        # print("BONE CHILDREN", bone_hierarchy)

    # ===================================
    # parse geometry
    # ===================================
    for obj in export_objects:
        element = generate_element(
            obj,
            parent=None,
            armature=armature,
            bone_hierarchy=bone_hierarchy,
            groups=groups,
            model_colors=model_colors,
            model_textures=model_textures,
            parent_cube_origin=np.array([0., 0., 0.]),     # root cube origin 
            parent_rotation_origin=np.array([0., 0., 0.]), # root rotation origin
            export_uvs=export_uvs,
        )

        if element is not None:
            root_elements.append(element)
    
    # ===========================
    # generate color texture image
    # ===========================
    if generate_texture:
        # default, get colors from all materials in file
        if model_colors is None:
            model_colors = set()
            for mat in bpy.data.materials:
                color = get_material_color(mat)
                if isinstance(color, tuple):
                    model_colors.add(color)
       
        tex_pixels, tex_size, color_tex_uv_map, default_color_uv = create_color_texture(model_colors)

        # texture output filepaths
        if color_texture_filename == "":
            current_dir = os.path.dirname(filepath)
            filepath_name = os.path.splitext(os.path.basename(filepath))[0]
            texture_save_path = os.path.join(current_dir, filepath_name + ".png")
            texture_model_path = posixpath.join(texture_folder, filepath_name)
        else:
            current_dir = os.path.dirname(filepath)
            texture_save_path = os.path.join(current_dir, color_texture_filename + ".png")
            texture_model_path = posixpath.join(texture_folder, color_texture_filename)
        
        # create + save texture
        tex = bpy.data.images.new("tex_colors", alpha=True, width=tex_size, height=tex_size)
        tex.file_format = "PNG"
        tex.pixels = tex_pixels
        tex.filepath_raw = texture_save_path
        tex.save()

        # write texture info to output model
        model_json["textureSizes"]["0"] = [tex_size, tex_size]
        model_json["textures"]["0"] = texture_model_path
    
    # if not generating texture, just write texture path to json file
    # TODO: scan materials for textures, then update output size
    elif color_texture_filename != "":
        model_json["textureSizes"]["0"] = [16, 16]
        model_json["textures"]["0"] = posixpath.join(texture_folder, color_texture_filename)
    
    # ===========================
    # process face texture paths
    # convert blender path names "//folder\tex.png" -> "{texture_folder}/tex"
    # add textures indices for textures, and create face mappings like "#1"
    # note: #0 id reserved for generated color texture
    # ===========================
    texture_refs = {} # maps blender path name -> #n identifiers
    texture_id = 1    # texture id in "#1" identifier
    for texture_path, texture_info in model_textures.items():
        texture_filename = texture_path
        if texture_filename[0:2] == "//":
            texture_filename = texture_filename[2:]
        texture_filename = texture_filename.replace("\\", "/")
        texture_filename = os.path.split(texture_filename)[1]
        texture_filename = os.path.splitext(texture_filename)[0]
        
        texture_refs[texture_path] = "#" + str(texture_id)
        model_json["textures"][str(texture_id)] = posixpath.join(texture_folder, texture_filename)
        model_json["textureSizes"][str(texture_id)] = texture_info.size
        texture_id += 1

    # ===========================
    # root object post-processing:
    # 1. recenter with origin shift
    # ===========================
    if translate_origin is not None:
        translate_origin = to_y_up(translate_origin)
        for element in root_elements:
            # re-centering
            element["to"] = translate_origin + element["to"]
            element["from"] = translate_origin + element["from"]
            element["rotationOrigin"] = translate_origin + element["rotationOrigin"]  
    
    # ===========================
    # all object post processing
    # 2. map solid color face uv -> location in generated texture
    # 3. rewrite path textures -> texture name reference
    # ===========================
    def final_element_processing(element):
        # convert numpy to python list
        element["to"] = element["to"].tolist()
        element["from"] = element["from"].tolist()
        element["rotationOrigin"] = element["rotationOrigin"].tolist()

        faces = element["faces"]
        for d, f in faces.items():
            if isinstance(f, tuple):
                color_uv = color_tex_uv_map[f] if f in color_tex_uv_map else default_color_uv
                faces[d] = {
                    "uv": color_uv,
                    "texture": "#0",
                }
            elif isinstance(f, dict):
                face_texture = f["texture"]
                if face_texture in texture_refs:
                    f["texture"] = texture_refs[face_texture]
                else:
                    face_texture = "#0"
        
        for child in element["children"]:
            final_element_processing(child)
        
    for element in root_elements:
        final_element_processing(element)
    
    # ===========================
    # convert groups
    # ===========================
    groups_export = []
    for g in groups:
        groups_export.append({
            "name": g,
            "origin": [0, 0, 0],
            "children": groups[g],
        })

    # save
    model_json["elements"] = root_elements
    model_json["groups"] = groups_export

    # ===========================
    # export animations
    # ===========================
    if export_animations:
        animations = save_all_animations()
        if len(animations) > 0:
            model_json["animations"] = animations

    # ===========================
    # minification options to reduce .json file size
    # ===========================
    indent = 2
    if minify == True:
        # remove json indent + newline
        indent= None

        # go through json dict and replace all float with rounded strings
        if decimal_precision >= 0:
            def round_float(x):
                return round(x, decimal_precision)
            
            def minify_element(elem):
                elem["from"] = [round_float(x) for x in elem["from"]]
                elem["to"] = [round_float(x) for x in elem["to"]]
                elem["rotationOrigin"] = [round_float(x) for x in elem["rotationOrigin"]]
                for face in elem["faces"].values():
                    face["uv"] = [round_float(x) for x in face["uv"]]
                
                for child in elem["children"]:
                    minify_element(child)

            for elem in model_json["elements"]:
                minify_element(elem)
    
    # save json
    with open(filepath, "w") as f:
        json.dump(model_json, f, separators=(",", ":"), indent=indent)


def save(
    context,
    filepath,
    selection_only = False,
    **kwargs,
):
    if selection_only:
        objects = filter_root_objects(bpy.context.selected_objects)
    else:
        objects = filter_root_objects(bpy.context.scene.collection.all_objects[:])

    save_objects(filepath, objects, **kwargs)

    print("SAVED", filepath)

    return {"FINISHED"}