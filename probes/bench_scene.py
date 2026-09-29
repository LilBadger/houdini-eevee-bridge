"""Write a reproducible USD benchmark scene (no Houdini or pxr module needed).

Floor mesh with face-varying UVs, spheres with Preview Surface materials, a
point instancer, a distant light, a dome light and a camera; Y-up, meters.
"""
import argparse
import math
from pathlib import Path


def fmt(values):
    return ', '.join(values)


def v3(x, y, z):
    return '(%g, %g, %g)' % (x, y, z)


def grid(n, size):
    points, counts, indices, uvs = [], [], [], []
    for j in range(n + 1):
        for i in range(n + 1):
            points.append(v3(-size / 2 + size * i / n, 0, -size / 2 + size * j / n))
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            quad = (a, a + n + 1, a + n + 2, a + 1)
            counts.append('4')
            indices.extend(map(str, quad))
            for k in quad:
                uvs.append('(%g, %g)' % ((k % (n + 1)) / n, (k // (n + 1)) / n))
    return points, counts, indices, uvs


def sphere(rings, segments, radius=1.):
    points = [v3(0, radius, 0)]
    for r in range(1, rings):
        phi = math.pi * r / rings
        for s in range(segments):
            theta = 2 * math.pi * s / segments
            points.append(v3(radius * math.sin(phi) * math.cos(theta), radius * math.cos(phi), radius * math.sin(phi) * math.sin(theta)))
    points.append(v3(0, -radius, 0))
    counts, indices = [], []
    for s in range(segments):
        counts.append('3'); indices += ['0', str(1 + (s + 1) % segments), str(1 + s)]
    for r in range(rings - 2):
        for s in range(segments):
            a, b = 1 + r * segments + s, 1 + r * segments + (s + 1) % segments
            counts.append('4'); indices += [str(a), str(b), str(b + segments), str(a + segments)]
    bottom = len(points) - 1
    base = 1 + (rings - 2) * segments
    for s in range(segments):
        counts.append('3'); indices += [str(bottom), str(base + s), str(base + (s + 1) % segments)]
    return points, counts, indices


def material(name, color, roughness, metallic):
    return '''
        def Material "%s"
        {
            token outputs:surface.connect = </World/Looks/%s/Surface.outputs:surface>
            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor = %s
                float inputs:roughness = %g
                float inputs:metallic = %g
                token outputs:surface
            }
        }''' % (name, name, v3(*color), roughness, metallic)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--floor', type=int, default=400, help='floor quads per side')
    parser.add_argument('--spheres', type=int, default=60)
    parser.add_argument('--instances', type=int, default=20000)
    args = parser.parse_args()
    fp, fc, fi, fuv = grid(args.floor, 40)
    sp, sc, si = sphere(24, 48)
    looks = [material('M%d' % i, (0.2 + 0.7 * ((i * 37) % 10) / 10, 0.2 + 0.7 * ((i * 53) % 10) / 10, 0.2 + 0.7 * ((i * 71) % 10) / 10),
                      0.15 + 0.08 * (i % 10), 1.0 if i % 4 == 0 else 0.0) for i in range(10)]
    spheres = []
    for i in range(args.spheres):
        angle = 2 * math.pi * i / args.spheres
        radius = 6 + 3 * (i % 3)
        spheres.append('''
        def Mesh "Sphere%d" (
            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {
            int[] faceVertexCounts = [%s]
            int[] faceVertexIndices = [%s]
            point3f[] points = [%s]
            uniform token subdivisionScheme = "none"
            rel material:binding = </World/Looks/M%d>
            double3 xformOp:translate = %s
            uniform token[] xformOpOrder = ["xformOp:translate"]
        }''' % (i, fmt(sc), fmt(si), fmt(sp), i % 10, v3(radius * math.cos(angle), 1, radius * math.sin(angle))))
    positions = []
    for k in range(args.instances):
        angle = 2 * math.pi * k / max(1, args.instances) * 7
        radius = 12 + 6 * (k % 97) / 97
        positions.append(v3(radius * math.cos(angle), 0.1 + 0.05 * (k % 13), radius * math.sin(angle)))
    text = '''#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
    startTimeCode = 1
    endTimeCode = 1
)

def Xform "World"
{
    def Scope "Looks"
    {%s
    }

    def Camera "Camera"
    {
        float focalLength = 35
        float horizontalAperture = 36
        float verticalAperture = 20.25
        float2 clippingRange = (0.1, 1000)
        double3 xformOp:translate = (0, 6, 24)
        float3 xformOp:rotateXYZ = (-12, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]
    }

    def DistantLight "Sun"
    {
        float inputs:intensity = 3
        float3 xformOp:rotateXYZ = (-50, 30, 0)
        uniform token[] xformOpOrder = ["xformOp:rotateXYZ"]
    }

    def DomeLight "Sky"
    {
        color3f inputs:color = (0.3, 0.35, 0.45)
        float inputs:intensity = 1
    }

    def Mesh "Floor" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        int[] faceVertexCounts = [%s]
        int[] faceVertexIndices = [%s]
        point3f[] points = [%s]
        texCoord2f[] primvars:st = [%s] (interpolation = "faceVarying")
        uniform token subdivisionScheme = "none"
        rel material:binding = </World/Looks/M1>
    }
%s

    def PointInstancer "Pebbles" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        point3f[] positions = [%s]
        int[] protoIndices = [%s]
        rel prototypes = [</World/Pebbles/Prototypes/Pebble>]
        rel material:binding = </World/Looks/M3>

        def Scope "Prototypes"
        {
            def Mesh "Pebble"
            {
                int[] faceVertexCounts = [4, 4, 4, 4, 4, 4]
                int[] faceVertexIndices = [0, 1, 3, 2, 2, 3, 5, 4, 4, 5, 7, 6, 6, 7, 1, 0, 1, 7, 5, 3, 6, 0, 2, 4]
                point3f[] points = [(-0.1, -0.1, 0.1), (0.1, -0.1, 0.1), (-0.1, 0.1, 0.1), (0.1, 0.1, 0.1), (-0.1, 0.1, -0.1), (0.1, 0.1, -0.1), (-0.1, -0.1, -0.1), (0.1, -0.1, -0.1)]
                uniform token subdivisionScheme = "none"
            }
        }
    }
}
''' % (''.join(looks), fmt(fc), fmt(fi), fmt(fp), fmt(fuv), ''.join(spheres), fmt(positions), fmt(['0'] * args.instances))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)
    print(args.output, len(text) // 1024, 'KiB')


if __name__ == '__main__':
    main()
