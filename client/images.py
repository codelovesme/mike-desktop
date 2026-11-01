"""Bounded PNG/JPEG attachments. Full decoding is repeated by Mike's API."""
import base64
import binascii

MAX_IMAGE = 2 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_COUNT = 3


def validate(images):
    if not isinstance(images, list) or len(images) > MAX_COUNT:
        raise ValueError('attach at most three PNG/JPEG images')
    total = 0
    for image in images:
        if not isinstance(image, dict) or set(image) != {'mime_type', 'data_base64'}:
            raise ValueError('image needs mime_type and data_base64')
        mime, encoded = image['mime_type'], image['data_base64']
        if mime not in ('image/png', 'image/jpeg') or not isinstance(encoded, str):
            raise ValueError('images must be PNG or JPEG')
        if len(encoded) > ((MAX_IMAGE+2)//3)*4:
            raise ValueError('each image must fit in 2 MiB')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError('image is not valid base64') from error
        if not data or len(data) > MAX_IMAGE:
            raise ValueError('each image must contain 1 byte to 2 MiB')
        if mime == 'image/png' and not (data.startswith(b'\x89PNG\r\n\x1a\n') and data[12:16] == b'IHDR' and data.endswith(b'IEND\xaeB`\x82')):
            raise ValueError('image content is not a PNG')
        if mime == 'image/jpeg' and not (data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9')):
            raise ValueError('image content is not a JPEG')
        total += len(data)
    if total > MAX_TOTAL:
        raise ValueError('attachments together must fit in 4 MiB')
    return images


def attachment(mime, data):
    value = {'mime_type': mime, 'data_base64': base64.b64encode(data).decode('ascii')}
    validate([value])
    return value
