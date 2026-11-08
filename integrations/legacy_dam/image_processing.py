"""COS processing command is a query KEY, not imageMogr2=<value>."""
def thumbnail_params(width):
 if isinstance(width,bool) or not isinstance(width,int) or not 1<=width<=4096:raise ValueError('invalid_thumbnail_width')
 return {f'imageMogr2/thumbnail/{width}x/':''}
