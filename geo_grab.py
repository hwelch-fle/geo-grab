from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Literal, get_args
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path
import webbrowser
import tempfile
import shutil
import zipfile
import json

try:
    from PIL import Image
    from PIL.ExifTags import Base, IFD, GPS
except (ModuleNotFoundError, ImportError):
    pass

if TYPE_CHECKING:
    from PIL import Image
    from PIL.ExifTags import Base, IFD, GPS


HAS_PIL = 'PIL' in sys.modules


# Internal type aliases
type GPSInfo = dict[int, Any]
type Latitude = float
type Longitude = float
type Coordinates = tuple[Latitude, Longitude]
type PointInfo = list[tuple[Path, Coordinates, float, str]]

# Literal options
Service = Literal['google', 'osm', 'apple', 'bing']
Services: tuple[Service, ...] = get_args(Service)
Format = Literal['shp', 'kml']
Formats: tuple[Format, ...] = get_args(Format)


# File constants
KML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml '
        'xmlns="http://www.opengis.net/kml/2.2" '
        'xmlns:gx="http://www.google.com/kml/ext/2.2" '
        'xmlns:kml="http://www.opengis.net/kml/2.2"'
    '>\n'
    '<Document>\n'
)
KML_FOOTER = (
    '</Document>\n'
    '</kml>\n'
)


def L(l: int) -> str:
    return '\t'*l


def _install_pillow():
    print(f'installing PIL for {sys.executable}')
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pillow'])
    sys.modules['PIL.Image'] = __import__('PIL', fromlist=['Image'])
    sys.modules['PIL.ExifTags'] = __import__('PIL', fromlist=['IFD', 'GPS'])


def dms_to_dd(d: float, m: float, s: float, b: Literal['N', 'S', 'E', 'W']) -> float:
    """Convert DMS coordiantes to Decimal Degrees

    Args:
        d: Degrees component
        m: Minutes component
        s: Seconds component
        b: Bearing of the coordinate (`NSEW`)

    Returns:
        The Lat/Lon value in Decimal Degrees (float)

    Example:
        ```python
        >>> dms_to_dd(95, 12, 100, 'E')
        95.22777777777777
    """
    dd = float(d) + float(m) / 60 + float(s) / 3600
    if b in 'NE':
        return dd
    else:
        return -1 * dd


def write_url(img_path: Path, url: str):
    with open(img_path.with_suffix('.url'), 'wt', encoding='utf-8') as u_file:
        u_file.write('[InternetShortcut]\n')
        u_file.write(f'URL={url}\n')


def write_kml(out_file: Path, info: PointInfo) -> None:
    with open(out_file, 'wt', encoding='utf-8') as kml:
        kml.write(KML_HEADER)
        kml.write(f'{L(1)}<name>{out_file.name}</name>\n')
        
        for img_path, (lat, lon), elev, timestamp in info:
            _rel_path = img_path.relative_to(out_file.parent)
            # Placemark container
            kml.write(
                f'{L(1)}<Placemark id="img_{img_path.name}">\n'
                    f'{L(2)}<name>{img_path.name}</name>\n'
            )
            
            # Description/Image/Timestamp
            kml.write(
                f'{L(2)}<description>\n'
                    f'{L(3)}<![CDATA['
                        '<img '
                        'style="max-width:500px;" '
                        f'src="{_rel_path}"'
                        '>'
                    ']]>\n'
                f'{L(2)}</description>\n'
                f'{L(2)}<TimeStamp>\n'
                    f'{L(3)}<when>{timestamp}</when>\n'
                f'{L(2)}</TimeStamp>\n'
            )

            # Style/Icon
            kml.write(
                f'{L(2)}<Style>\n'
                    f'{L(3)}<IconStyle>\n'
                        f'{L(4)}<scale>2.5</scale>\n'
                        f'{L(4)}<Icon>\n'
                            f'{L(5)}<href>{_rel_path}</href>\n'
                        f'{L(4)}</Icon>\n'
                    f'{L(3)}</IconStyle>\n'
                f'{L(2)}</Style>\n'
            )
            
            # Point
            kml.write(
                f'{L(2)}<Point>\n'
                    f'{L(3)}<gx:altitudeMode>clampToGround</gx:altitudeMode>\n'
                    f'{L(3)}<coordinates>{lon},{lat},{elev}</coordinates>\n'
                f'{L(2)}</Point>\n'
            )
            kml.write(f'{L(1)}</Placemark>\n')
        kml.write(KML_FOOTER)


def write_kmz(out_file: Path, info: PointInfo) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        kml = Path(tmp) / f'{out_file.stem}.kml'
        files = Path(tmp) / 'files'
        files.mkdir()
        for img, *_ in info:
            img_dest = files / img.name
            shutil.copyfile(img, img_dest)
        
        kmz_info = [(files / i[0].name, *i[1:])for i in info]
        write_kml(kml, kmz_info)
        
        with zipfile.ZipFile(out_file, 'w') as kmz:
            kmz.write(kml, kml.relative_to(tmp))
            for fl in files.iterdir():
                kmz.write(fl, fl.relative_to(tmp))


def write_geojson(out_file: Path, info: PointInfo) -> None:
    _json: dict[str, Any] = {
        'type': 'FeatureCollection',
        'features': [
            {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [lon, lat, elev]
                },
                'properties': {
                    'timestamp': timestamp,
                    'filepath': str(img),
                }
            }
            for img, (lat, lon), elev, timestamp in info
        ],
    }
    with open(out_file, 'wt', encoding='utf-8') as geojson:
        json.dump(_json, geojson, indent=2)


def write_csv(out_file: Path, info: PointInfo) -> None:
    with open(out_file, 'wt', encoding='utf-8') as csv:
        csv.write('Name,Latitude,Longitude,Elevation,Timestamp,Filepath\n')
        for img, (lat, lon), elev, timestamp in info:
            csv.write(f'{img.name},{lat},{lon},{elev},{timestamp},{img.resolve()}\n')


def get_lat_lon_elev(gps_info: GPSInfo) -> tuple[Coordinates, float]:
    lat = dms_to_dd(
        *gps_info.get(GPS.GPSLatitude, [float()] * 3),
        b=gps_info.get(GPS.GPSLatitudeRef, 'N'),
    )
    lon = dms_to_dd(
        *gps_info.get(GPS.GPSLongitude, [float()] * 3),
        b=gps_info.get(GPS.GPSLongitudeRef, 'E'),
    )
    elev = float(gps_info.get(GPS.GPSAltitude, 0))
    return (lat, lon), elev


def format_url(lat: float, lon: float, service: Service = 'google') -> str:
    match service:
        case 'google':
            return f'https://www.google.com/maps/search/?api=1&query={lat},{lon}'
        case 'osm':
            return f'https://www.openstreetmap.org/search?query={lat}%2F{lon}#map=13/{lat}/{lon}'
        case 'apple':
            return f'https://maps.apple.com/frame?center={lat}%252C{lon}'
        case 'bing':
            return f'https://www.bing.com/maps/search?style=r&q={lat}%2C+{lon}&lvl=16&style=r'


def open_url(url: str) -> None:
    webbrowser.open(url)


def get_geo_exif(img_path: Path) -> GPSInfo:
    exif = Image.open(img_path).getexif()
    gps_info = exif.get_ifd(IFD.GPSInfo)
    time_info = {Base.DateTime: exif.get(Base.DateTime)}
    return (gps_info | time_info) if gps_info else {}


def get_info(folder: Path) -> Iterator[tuple[Path, GPSInfo | None]]:
    for img in folder.glob('*.jp*g'):
        yield img, get_geo_exif(img) or None


def main(
    directory: Path,
    verbose: bool,
    open_: bool,
    w_url: bool,
    services: tuple[Service, ...],
    kml_name: str | None,
    kmz_name: str | None,
    geojson_name: str | None,
    csv_name: str | None,
):
    _files: PointInfo = []
    if w_url and verbose:
        print(f'writing urls for {services}...')
    if open_ and verbose:
        print(f'opening tabs for {services}...')
    
    for img, geo_info in get_info(directory):
        if not geo_info:
            continue
        (lat, lon), elev = get_lat_lon_elev(geo_info)
        _files.append((img, (lat, lon), elev, str(geo_info[Base.DateTime])))
        if verbose:
            print(f'{img.name}: {lat}, {lon}')
        urls = {s: format_url(lat, lon, s) for s in services if s in Services}
        for service, url in urls.items():
            if open_:
                webbrowser.open(url)
            if w_url:
                service_path = img.with_stem(f'{img.stem}_{service}')
                write_url(service_path, url)
    if kml_name is not None:
        if verbose:
            print('writing kml...')
        write_kml((directory / kml_name).with_suffix('.kml'), _files)
    if kmz_name is not None:
        if verbose:
            print('writing kmz...')
        write_kmz((directory / kmz_name).with_suffix('.kmz'), _files)
    if geojson_name is not None:
        if verbose:
            print('writing geojson...')
        write_geojson((directory / geojson_name).with_suffix('.geojson'), _files)
    if csv_name is not None:
        if verbose:
            print('writing csv...')
        write_csv((directory / csv_name).with_suffix('.csv'), _files)


if __name__ == '__main__':
    parser = ArgumentParser('GeoTag Getter')
    parser.add_argument(
        'directory',
        help='Image directory to geolocate',
        type=Path,
    )
    parser.add_argument(
        '-v',
        '--verbose',
        help='Set verbosity level on command line',
        action='store_true',
    )
    parser.add_argument(
        '-o',
        '--open',
        help='Open a browser tab for each image location and service',
        action='store_true',
    )
    parser.add_argument(
        '-u',
        '--url',
        help='Write the link(s) to Windows url file(s) [one file per chosen service]',
        action='store_true',
    )
    parser.add_argument(
        '-k',
        '--kml',
        help='If populated, write a kml file with the given name',
        type=str,
        default=None,
    )
    parser.add_argument(
        '-z',
        '--kmz',
        help='If populated, write a kmz file with the given name',
        type=str,
        default=None,
    )
    parser.add_argument(
        '-g',
        '--geojson',
        help='If populated, write a geojson file with the given name',
        type=str,
        default=None,
    )
    parser.add_argument(
        '-c',
        '--csv',
        help='If populated, write a csv file with the given name',
        type=str,
        default=None,
    )
    parser.add_argument(
        '--services',
        help=f'Optional webmap service to use for location [{Services}]',
        nargs='+',
        default=['google'],
        type=str,
    )
    if not HAS_PIL:
        parser.add_argument(
            '-i',
            '--install',
            help='Install pillow dependency',
            action='store_true',
        )
    
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    
    args = parser.parse_args()

    if not HAS_PIL and args.install:
        _install_pillow()
        print('pillow installed, please run the script again')
        sys.exit(1)

    services: tuple[Service, ...] = tuple(args.services) if 'all' not in args.services else Services
    main(
        Path(args.directory),
        # Flags
        args.verbose,
        args.open,
        
        # URL/Browser
        args.url,        
        services,
        
        args.kml,
        args.kmz,
        args.geojson,
        args.csv,
    )
