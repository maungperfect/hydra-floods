import ee
from ee.ee_exception import EEException
import math
import string
import random
import datetime

try:
    ee.Initialize()
except EEException as e:
    from oauth2client.service_account import ServiceAccountCredentials
    credentials = ServiceAccountCredentials.from_p12_keyfile(
    service_account_email='',
    filename='',
    )
    ee.Initialize(credentials)

landShp = ee.FeatureCollection('USDOS/LSIB/2013')
S1_polygons = ee.FeatureCollection('projects/servir-mekong/hydrafloods/S1_polygons')

# helper function to convert qa bit image to flag
def extractBits(image, start, end, newName):
    # Compute the bits we need to extract.
    pattern = 0
    for i in range(start,end):
       pattern += int(math.pow(2, i))

    # Return a single band image of the extracted QA bits, giving the band
    # a new name.
    return image.select([0], [newName])\
                  .bitwiseAnd(pattern)\
                  .rightShift(start)


def getTileLayerUrl(ee_image_object):
    map_id = ee.Image(ee_image_object).getMapId()
    tile_url_template = "https://earthengine.googleapis.com/map/{mapid}/{{z}}/{{x}}/{{y}}?token={token}"
    return tile_url_template.format(**map_id)


def exportImage(image,region,assetId,description=None,scale=90,crs='EPSG:4326'):
    if (description == None) or (type(description) != str):
        description = ''.join(random.SystemRandom().choice(
        string.ascii_letters) for _ in range(8)).lower()
    # get serializable geometry for export
    exportRegion = region.bounds().getInfo()['coordinates']

    # set export process
    export = ee.batch.Export.image.toAsset(image,
      description = description,
      assetId = assetId,
      scale = scale,
      region = exportRegion,
      maxPixels = 1e13,
      crs = crs
    )
    # start export process
    export.start()

    return

def otsu_function(histogram):
    counts = ee.Array(ee.Dictionary(histogram).get('histogram'))
    means = ee.Array(ee.Dictionary(histogram).get('bucketMeans'))
    size = means.length().get([0])
    total = counts.reduce(ee.Reducer.sum(), [0]).get([0])
    sums = means.multiply(counts).reduce(ee.Reducer.sum(), [0]).get([0])
    mean = sums.divide(total)
    indices = ee.List.sequence(1, size)
    #Compute between sum of squares, where each mean partitions the data.

    def bss_function(i):
        aCounts = counts.slice(0, 0, i)
        aCount = aCounts.reduce(ee.Reducer.sum(), [0]).get([0])
        aMeans = means.slice(0, 0, i)
        aMean = aMeans.multiply(aCounts).reduce(ee.Reducer.sum(), [0]).get([0]).divide(aCount)
        bCount = total.subtract(aCount)
        bMean = sums.subtract(aCount.multiply(aMean)).divide(bCount)
        return aCount.multiply(aMean.subtract(mean).pow(2)).add(
               bCount.multiply(bMean.subtract(mean).pow(2)))

    bss = indices.map(bss_function)
    output = means.sort(bss).get([-1])
    return output

def globalOtsu(collection,target_date,region,
               canny_threshold=7,    # threshold for canny edge detection
               canny_sigma=1,        # sigma value for gaussian filter
               canny_lt=7,           # lower threshold for canny detection
               smoothing=100,        # amount of smoothing in meters
               connected_pixels=200, # maximum size of the neighborhood in pixels
               edge_length=50,       # minimum length of edges from canny detection
               smooth_edges=100,
               qualityBand=None,
               reductionScale=90,
               initThresh=0,
               seed=7):

    tDate = ee.Date(target_date)
    targetColl = collection.filterDate(tDate,tDate.advance(1,'day'))

    if qualityBand == None:
        histBand = ee.String(target.bandNames().get(0))
        target = targetColl.mosaic()\
            .select(histBand)
    else:
        histBand = ee.String(qualityBand)
        target = targetColl.qualityMosaic(qualityBand)\
            .select(histBand)

    binary = target.gt(initThresh).rename('binary')

    samps = binary.stratifiedSample(numPoints=20,
        classBand='binary',
        region=region,
        scale=reductionScale,
        geometries=True,
        seed=seed,
        tileScale=16
    )
    sampleRegion = samps.geometry().buffer(2500)

    canny = ee.Algorithms.CannyEdgeDetector(target,canny_threshold,canny_sigma)

    connected = canny.mask(canny).lt(canny_lt).connectedPixelCount(connected_pixels, True)
    edges = connected.gte(edge_length)

    edgeBuffer = edges.focal_max(smooth_edges, 'square', 'meters')

    histogram_image = target.mask(edgeBuffer)

    histogram =  histogram_image.reduceRegion(ee.Reducer.histogram(255, 2)\
                                .combine('mean', None, True)\
                                .combine('variance', None,True),sampleRegion,reductionScale,bestEffort=True,
                                tileScale=16)

    threshold = otsu_function(histogram.get(histBand.cat('_histogram')))

    water = target.gt(threshold).clip(landShp.geometry())

    return water

def bootstrapOtsu(collection,target_date,
                  neg_buffer=-1500,     # negative buffer for masking potential bad data
                  upper_threshold=-14,  # upper limit for water threshold
                  canny_threshold=7,    # threshold for canny edge detection
                  canny_sigma=1,        # sigma value for gaussian filter
                  canny_lt=7,           # lower threshold for canny detection
                  smoothing=100,        # amount of smoothing in meters
                  connected_pixels=200, # maximum size of the neighborhood in pixels
                  edge_length=50,       # minimum length of edges from canny detection
                  smooth_edges=100,
                  qualityBand=None,
                  reductionScale=90):

    tDate = ee.Date(target_date)
    targetColl = collection.filterDate(tDate,tDate.advance(1,'day'))

    nImgs = targetColl.size().getInfo()
    if nImgs <= 0:
        raise EEException('Selected date has no imagery, please try processing another date')

    collGeom = targetColl.geometry()
    polygons = S1_polygons.filterBounds(collGeom)

    nPolys = polygons.size().getInfo()
    if nPolys > 0:
        ids = ee.List(polygons.aggregate_array('id'))
        random_ids = []
        for i in range(3):
            random_ids.append(random.randint(0, ids.size().subtract(1).getInfo()))
        random_ids = ee.List(random_ids)

        def getRandomIds(i):
            return ids.get(i)

        ids = random_ids.map(getRandomIds)
        polygons = polygons.filter(ee.Filter.inList('id', ids))

        if qualityBand == None:
            target   = targetColl.mosaic().set('system:footprint', collGeom.dissolve())
            target   = target.clip(target.geometry().buffer(neg_buffer))
            smoothed = target.focal_median(smoothing, 'circle', 'meters')
            histBand = ee.String(target.bandNames().get(0))
        else:
            target   = targetColl.qualityMosaic(qualityBand).set('system:footprint', collGeom.dissolve())
            target   = target.clip(target.geometry().buffer(neg_buffer))
            smoothed = target.focal_median(smoothing, 'circle', 'meters')
            histBand = ee.String(qualityBand)

        canny = ee.Algorithms.CannyEdgeDetector(smoothed,canny_threshold,canny_sigma)

        connected = canny.mask(canny).lt(canny_lt).connectedPixelCount(connected_pixels, True)
        edges = connected.gte(edge_length)

        edgeBuffer = edges.focal_max(smooth_edges, 'square', 'meters')

        histogram_image = smoothed.mask(edgeBuffer)
        histogram = histogram_image.reduceRegion(ee.Reducer.histogram(255, 2),polygons.geometry(),reductionScale,bestEffort=True)

        threshold = ee.Number(otsu_function(histogram.get(histBand))).min(upper_threshold)
    else:
        threshold = upper_threshold

    water = smoothed.lt(threshold).clip(landShp.geometry())

    return water

def rescaleBands(img):
    def individualBand(b):
        b = ee.String(b)
        minKey = b.cat('_min')
        maxKey = b.cat('_max')
        return img.select(b).unitScale(
            ee.Number(minMax.get(minKey)),
            ee.Number(minMax.get(maxKey)))

    bandNames = img.bandNames()
    geom = img.geometry()

    minMax = img.reduceRegion(
        reducer = ee.Reducer.minMax(),
        geometry = geom,
        scale = 90,
        bestEffort = True
    )

    rescaled = bandNames.map(individualBand)
    bandSequence = ee.List.sequence(0,bandNames.length().subtract(1))

    return ee.ImageCollection(rescaled).toBands().select(bandSequence,bandNames)


def logitTransform(img):
    return img.divide(ee.Image(1).subtract(img)).log()


def toNatural(img):
    return ee.Image(10.0).pow(img.select(0).divide(10.0))


def toDB(img):
    return ee.Image(img).log10().multiply(10.0)


def despeckle(img):
  """ Refined Lee Speckle Filter """
  t = ee.Date(img.get('system:time_start'))
  # angles = img.select('angle')
  geom = img.geometry()
  # The RL speckle filter
  img = toNatural(img)
  # img must be in natural units, i.e. not in dB!
  # Set up 3x3 kernels
  weights3 = ee.List.repeat(ee.List.repeat(1,3),3)
  kernel3 = ee.Kernel.fixed(3,3, weights3, 1, 1, False)

  mean3 = img.reduceNeighborhood(ee.Reducer.mean(), kernel3)
  variance3 = img.reduceNeighborhood(ee.Reducer.variance(), kernel3)

  # Use a sample of the 3x3 windows inside a 7x7 windows to determine gradients and directions
  sample_weights = ee.List([[0,0,0,0,0,0,0], [0,1,0,1,0,1,0],[0,0,0,0,0,0,0],\
                            [0,1,0,1,0,1,0], [0,0,0,0,0,0,0], [0,1,0,1,0,1,0],[0,0,0,0,0,0,0]])

  sample_kernel = ee.Kernel.fixed(7,7, sample_weights, 3,3, False)

  # Calculate mean and variance for the sampled windows and store as 9 bands
  sample_mean = mean3.neighborhoodToBands(sample_kernel)
  sample_var = variance3.neighborhoodToBands(sample_kernel)

  # Determine the 4 gradients for the sampled windows
  gradients = sample_mean.select(1).subtract(sample_mean.select(7)).abs()
  gradients = gradients.addBands(sample_mean.select(6).subtract(sample_mean.select(2)).abs())
  gradients = gradients.addBands(sample_mean.select(3).subtract(sample_mean.select(5)).abs())
  gradients = gradients.addBands(sample_mean.select(0).subtract(sample_mean.select(8)).abs())

  # And find the maximum gradient amongst gradient bands
  max_gradient = gradients.reduce(ee.Reducer.max())

  # Create a mask for band pixels that are the maximum gradient
  gradmask = gradients.eq(max_gradient)

  # duplicate gradmask bands: each gradient represents 2 directions
  gradmask = gradmask.addBands(gradmask)

  # Determine the 8 directions
  directions = sample_mean.select(1).subtract(sample_mean.select(4)).gt(sample_mean.select(4).\
                                  subtract(sample_mean.select(7))).multiply(1)

  directions = directions.addBands(sample_mean.select(6).subtract(sample_mean.select(4)).\
                                   gt(sample_mean.select(4).subtract(sample_mean.select(2))).multiply(2))

  directions = directions.addBands(sample_mean.select(3).subtract(sample_mean.select(4)).\
                                   gt(sample_mean.select(4).subtract(sample_mean.select(5))).multiply(3))

  directions = directions.addBands(sample_mean.select(0).subtract(sample_mean.select(4)).\
                                   gt(sample_mean.select(4).subtract(sample_mean.select(8))).multiply(4))
  # The next 4 are the not() of the previous 4
  directions = directions.addBands(directions.select(0).Not().multiply(5))
  directions = directions.addBands(directions.select(1).Not().multiply(6))
  directions = directions.addBands(directions.select(2).Not().multiply(7))
  directions = directions.addBands(directions.select(3).Not().multiply(8))

  # Mask all values that are not 1-8
  directions = directions.updateMask(gradmask)

  # "collapse" the stack into a singe band image (due to masking, each pixel has just one value (1-8) in it's directional band, and is otherwise masked)
  directions = directions.reduce(ee.Reducer.sum())

  sample_stats = sample_var.divide(sample_mean.multiply(sample_mean))

  # Calculate localNoiseVariance
  sigmaV = sample_stats.toArray().arraySort().arraySlice(0,0,5).arrayReduce(ee.Reducer.mean(), [0])

  # Set up the 7*7 kernels for directional statistics
  rect_weights = ee.List.repeat(ee.List.repeat(0,7),3).cat(ee.List.repeat(ee.List.repeat(1,7),4))

  diag_weights = ee.List([[1,0,0,0,0,0,0], [1,1,0,0,0,0,0], [1,1,1,0,0,0,0],\
                          [1,1,1,1,0,0,0], [1,1,1,1,1,0,0], [1,1,1,1,1,1,0], [1,1,1,1,1,1,1]])

  rect_kernel = ee.Kernel.fixed(7,7, rect_weights, 3, 3, False)
  diag_kernel = ee.Kernel.fixed(7,7, diag_weights, 3, 3, False)

  # Create stacks for mean and variance using the original kernels. Mask with relevant direction.
  dir_mean = img.reduceNeighborhood(ee.Reducer.mean(), rect_kernel).updateMask(directions.eq(1))
  dir_var = img.reduceNeighborhood(ee.Reducer.variance(), rect_kernel).updateMask(directions.eq(1))

  dir_mean = dir_mean.addBands(img.reduceNeighborhood(ee.Reducer.mean(), diag_kernel).updateMask(directions.eq(2)))
  dir_var = dir_var.addBands(img.reduceNeighborhood(ee.Reducer.variance(), diag_kernel).updateMask(directions.eq(2)))

  # and add the bands for rotated kernels
  i = 1
  while i < 4:
    dir_mean = dir_mean.addBands(img.reduceNeighborhood(ee.Reducer.mean(), rect_kernel.rotate(i)).updateMask(directions.eq(2*i+1)))
    dir_var = dir_var.addBands(img.reduceNeighborhood(ee.Reducer.variance(), rect_kernel.rotate(i)).updateMask(directions.eq(2*i+1)))
    dir_mean = dir_mean.addBands(img.reduceNeighborhood(ee.Reducer.mean(), diag_kernel.rotate(i)).updateMask(directions.eq(2*i+2)))
    dir_var = dir_var.addBands(img.reduceNeighborhood(ee.Reducer.variance(), diag_kernel.rotate(i)).updateMask(directions.eq(2*i+2)))
    i+=1
  # "collapse" the stack into a single band image (due to masking, each pixel has just one value in it's directional band, and is otherwise masked)
  dir_mean = dir_mean.reduce(ee.Reducer.sum())
  dir_var = dir_var.reduce(ee.Reducer.sum())

  # A finally generate the filtered value
  varX = dir_var.subtract(dir_mean.multiply(dir_mean).multiply(sigmaV)).divide(sigmaV.add(1.0))

  b = varX.divide(dir_var)

  # Get a multi-band image bands.
  result = dir_mean.add(b.multiply(img.subtract(dir_mean))).arrayProject([0])\
    .arrayFlatten([['sum']])\
    .float()


  return toDB(result)

def addIndices(img):
    ndvi = img.normalizedDifference(['nir','red']).rename('ndvi')
    mndwi = img.normalizedDifference(['green','swir1']).rename('mndwi')
    nwi = img.expression('((b-(n+s+w))/(b+(n+s+w))*100)',{
        'b':img.select('blue'),
        'n':img.select('nir'),
        's':img.select('swir1'),
        'w':img.select('swir2')
    }).rename('nwi')
    aewinsh = img.expression('4.0 * (g-s) - ((0.25*n) + (2.75*w))',{
        'g':img.select('green'),
        's':img.select('swir1'),
        'n':img.select('nir'),
        'w':img.select('swir2')
    }).rename('aewinsh')
    aewish = img.expression('b+2.5*g-1.5*(n+s)-0.25*w',{
        'b':img.select('blue'),
        'g':img.select('green'),
        'n':img.select('nir'),
        's':img.select('swir1'),
        'w':img.select('swir2')
    }).rename('aewish')
    tcwet = img.expression('0.1509*b + 0.1973*g + 0.3279*r + 0.3406*n - 0.7112*s - 0.4572*w',{
        'b':img.select('blue'),
        'g':img.select('green'),
        'r':img.select('red'),
        'n':img.select('nir'),
        's':img.select('swir1'),
        'w':img.select('swir2')
    }).rename('tcwet')

    return ee.Image.cat([img,ndvi,mndwi,nwi,aewinsh,aewish,tcwet])


def SurfaceWaterAlgorithm(aoi,images, pcnt_perm, pcnt_temp, water_thresh, ndvi_thresh, hand_mask):

    STD_NAMES   = ['blue2', 'blue', 'green', 'red', 'nir', 'swir1', 'swir2']

    # calculate percentile images
    prcnt_img_perm = images.reduce(ee.Reducer.percentile([float(pcnt_perm)])).rename(STD_NAMES)
    prcnt_img_temp = images.reduce(ee.Reducer.percentile([float(pcnt_temp)])).rename(STD_NAMES)

    # MNDWI
    MNDWI_perm = prcnt_img_perm.normalizedDifference(['green', 'swir1'])
    MNDWI_temp = prcnt_img_temp.normalizedDifference(['green', 'swir1'])

    # water
    water_perm = MNDWI_perm.gt(float(water_thresh))
    water_temp = MNDWI_temp.gt(float(water_thresh))

    # get NDVI masks
    NDVI_perm_pcnt = prcnt_img_perm.normalizedDifference(['nir', 'red'])
    NDVI_temp_pcnt = prcnt_img_temp.normalizedDifference(['nir', 'red'])
    NDVI_mask_perm = NDVI_perm_pcnt.gt(float(ndvi_thresh))
    NDVI_mask_temp = NDVI_temp_pcnt.gt(float(ndvi_thresh))

    # combined NDVI and HAND masks
    full_mask_perm = NDVI_mask_perm.add(hand_mask)
    full_mask_temp = NDVI_mask_temp.add(hand_mask)

    # apply NDVI and HAND masks
    water_perm_masked = water_perm.updateMask(full_mask_perm.Not())
    water_temp_masked = water_temp.updateMask(full_mask_perm.Not())

    # single image with permanent and temporary water
    water_complete = water_perm_masked.add(water_temp_masked).clip(aoi)

    #return water_complete.updateMask(water_complete)
    return water_complete

def getLandsatCollection(aoi,time_start, time_end, month_index=None, climatology=True, defringe=True, cloud_thresh=None):

    # Landsat band names
    LC457_BANDS = ['B1',    'B1',   'B2',    'B3',  'B4',  'B5',    'B7']
    LC8_BANDS   = ['B1',    'B2',   'B3',    'B4',  'B5',  'B6',    'B7']
    STD_NAMES   = ['blue2', 'blue', 'green', 'red', 'nir', 'swir1', 'swir2']


    # filter Landsat collections on bounds and dates
    L4 = ee.ImageCollection('LANDSAT/LT04/C01/T1_TOA').filterBounds(aoi).filterDate(time_start, ee.Date(time_end).advance(1, 'day'))
    L5 = ee.ImageCollection('LANDSAT/LT05/C01/T1_TOA').filterBounds(aoi).filterDate(time_start, ee.Date(time_end).advance(1, 'day'))
    L7 = ee.ImageCollection('LANDSAT/LE07/C01/T1_TOA').filterBounds(aoi).filterDate(time_start, ee.Date(time_end).advance(1, 'day'))
    L8 = ee.ImageCollection('LANDSAT/LC08/C01/T1_TOA').filterBounds(aoi).filterDate(time_start, ee.Date(time_end).advance(1, 'day'))

    # apply cloud masking
    if int(cloud_thresh) >= 0:
        # helper function: cloud busting
        # (https://code.earthengine.google.com/63f075a9e212f6ed4770af44be18a4fe, Ian Housman and Carson Stam)
        def bustClouds(img):
            t = img
            cs = ee.Algorithms.Landsat.simpleCloudScore(img).select('cloud')
            out = img.mask(img.mask().And(cs.lt(ee.Number(int(cloud_thresh)))))
            return out.copyProperties(t)
        # apply cloud busting function
        L4 = L4.map(bustClouds)
        L5 = L5.map(bustClouds)
        L7 = L7.map(bustClouds)
        L8 = L8.map(bustClouds)

    # select bands and rename
    L4 = L4.select(LC457_BANDS, STD_NAMES)
    L5 = L5.select(LC457_BANDS, STD_NAMES)
    L7 = L7.select(LC457_BANDS, STD_NAMES)
    L8 = L8.select(LC8_BANDS, STD_NAMES)

    # apply defringing
    if defringe == 'true':
        # helper function: defringe Landsat 5 and/or 7
        # (https://code.earthengine.google.com/63f075a9e212f6ed4770af44be18a4fe, Bonnie Ruefenacht)
        k = ee.Kernel.fixed(41, 41, \
        [[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], \
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]])
        fringeCountThreshold = 279  # define number of non null observations for pixel to not be classified as a fringe
        def defringeLandsat(img):
            m   = img.mask().reduce(ee.Reducer.min())
            sum = m.reduceNeighborhood(ee.Reducer.sum(), k, 'kernel')
            sum = sum.gte(fringeCountThreshold)
            img = img.mask(img.mask().And(sum))
            return img
        L5 = L5.map(defringeLandsat)
        L7 = L7.map(defringeLandsat)

    # merge collections
    images = ee.ImageCollection(L4.merge(L5).merge(L7).merge(L8))

    # filter on selected month
    if climatology:
        if month_index != None:
            images = images.filter(ee.Filter.calendarRange(int(month_index), int(month_index), 'month'))
        else:
            raise ValueError('Month needs to be defined to calculate climatology')

    return images

def JRCAlgorithm(geom,startDate, endDate, month=None):

    IMAGE_COLLECTION = ee.ImageCollection('JRC/GSW1_0/MonthlyHistory')

    myjrc = IMAGE_COLLECTION.filterBounds(geom).filterDate(startDate, endDate)

    if month != None:
        myjrc = myjrc.filter(ee.Filter.calendarRange(int(month), int(month), 'month'))

    # calculate total number of observations
    def calcObs(img):
        # observation is img > 0
        obs = img.gt(0)
        return ee.Image(obs).set('system:time_start', img.get('system:time_start'))

    # calculate the number of times water
    def calcWater(img):
        water = img.select('water').eq(2)
        return ee.Image(water).set('system:time_start', img.get('system:time_start'))

    observations = myjrc.map(calcObs)

    water = myjrc.map(calcWater)

    # sum the totals
    totalObs = ee.Image(ee.ImageCollection(observations).sum().toFloat())
    totalWater = ee.Image(ee.ImageCollection(water).sum().toFloat())

    # calculate the percentage of total water
    returnTime = totalWater.divide(totalObs).multiply(100)

    # make a mask
    water = returnTime.gt(75).rename(['water'])
    water = water.updateMask(water)

    return water


def getHistoricalMap(geom,iniTime,endTime,
                  climatology=True,
                  month=None,
                  defringe=True,
                  pcnt_perm=40,
                  pcnt_temp=8,
                  water_thresh=0.35,
                  ndvi_thresh=0.5,
                  hand_thresh=30,
                  cloud_thresh=10,
                  algorithm='SWT'):

    def spatialSelect(feature):
        test = ee.Algorithms.If(geom.contains(feature.geometry()),feature,None)
        return ee.Feature(test)

    countries = landShp.filterBounds(geom).map(spatialSelect,True)

    if climatology:
        if month == None:
            raise ValueError('Month needs to be defined to calculate climatology')

    if algorithm == 'SWT':
        # get images
        images = getLandsatCollection(geom,iniTime, endTime, climatology, month, defringe, cloud_thresh)

        # Height Above Nearest Drainage (HAND)
        HAND = ee.Image('users/arjenhaag/SERVIR-Mekong/HAND_MERIT')

        # get HAND mask
        HAND_mask = HAND.gt(float(hand_thresh))


        water = SurfaceWaterAlgorithm(geom,images, pcnt_perm, pcnt_temp, water_thresh, ndvi_thresh, HAND_mask).clip(countries)
        waterMap = getTileLayerUrl(water.updateMask(water.eq(2)).visualize(min=0,max=2,palette='#ffffff,#9999ff,#00008b'))

    elif algorithm == 'JRC':
        water = JRCAlgorithm(geom,iniTime,endTime).clip(countries)
        waterMap = getTileLayerUrl(water.visualize(min=0,max=1,bands='water',palette='#ffffff,#00008b'))

    else:
        raise NotImplementedError('Selected algorithm string not available. Options are: "SWT" or "JRC"')

    return waterMap


def _accumulate(ic,today,ndays=1):

    eeDate = ee.Date(today)

    ic_filtered = ic.filterDate(eeDate.advance(-ndays,'day'),eeDate)

    accum_img = ee.Image(ic_filtered.sum())

    return accum_img.updateMask(accum_img.gt(1))

def getPrecipMap(accumulation=1):
    if accumulation not in [1,3,7]:
        raise NotImplementedError('Selected accumulation value is not yet impleted, options are: 1, 3, 7')

    dt = datetime.datetime.utcnow() - datetime.timedelta(1)
    today = dt.strftime('%Y-%m-%d')

    ic = ee.ImageCollection('JAXA/GPM_L3/GSMaP/v6/operational').select(['hourlyPrecipRateGC'])

    ranges = {1:[1,100],3:[1,250],7:[1,500]}
    crange = ranges[accumulation]

    accum = _accumulate(ic,today,accumulation)

    precipMap = getTileLayerUrl(accum.visualize(min=crange[0],max=crange[1],
                                                palette='#000080,#0045ff,#00fbb2,#67d300,#d8ff22,#ffbe0c,#ff0039,#c95df5,#fef8fe'
                                               )
                               )
    return precipMap

def getAdminMap(geom):
    def spatialSelect(feature):
        test = ee.Algorithms.If(geom.contains(feature.geometry()),feature,None)
        return ee.Feature(test)

    countries = ee.FeatureCollection('USDOS/LSIB_SIMPLE/2017').map(spatialSelect,True)

    # Create an empty image into which to paint the features, cast to byte.
    empty = ee.Image().byte()

    # Paint all the polygon edges with the same number and width, display.
    outline = empty.paint(
      featureCollection=countries,
      width=2
    )

    return getTileLayerUrl(outline.visualize())
