import director.vtkAll as vtk
import director.objectmodel as om
from director import lcmUtils

# if bot_lcmgl cannot be important than this module will not be able to
# support lcmgl, but it can still be imported in a disabled state
try:
    import bot_lcmgl
    import octomap as lcmOctomap
    LCMGL_AVAILABLE = True
except ImportError:
    LCMGL_AVAILABLE = False


class OctomapObject(om.ObjectModelItem):

    def __init__(self, name, actor):

        om.ObjectModelItem.__init__(self, name, om.Icons.Octomap)


        self.actor = actor
        self.actor.SetUseBounds(False)
        self.addProperty('Visible', actor.GetVisibility())
        self.addProperty('Color Mode', 0, attributes=om.PropertyAttributes(enumNames=['Drab', 'Height']))
        self.addProperty('Alpha', 0.8,
                         attributes=om.PropertyAttributes(decimals=2, minimum=0, maximum=1.0, singleStep=0.1, hidden=False))
        self.views = []

    def _onPropertyChanged(self, propertySet, propertyName):
        om.ObjectModelItem._onPropertyChanged(self, propertySet, propertyName)

        if propertyName == 'Visible':
            self.actor.SetVisibility(self.getProperty(propertyName))
            self.renderAllViews()

        elif propertyName == 'Alpha':
            self.actor.SetAlphaOccupied(self.getProperty(propertyName))
            self.renderAllViews()

        elif propertyName == 'Color Mode':
            arrayMap = {
              0 : 'Drab',
              1 : 'Height'
            }
            heightColorMode = self.getProperty(propertyName)
            arrayName = arrayMap.get(heightColorMode)
            if (arrayName is 'Height'):
                self.actor.SetHeightColorMode(True)
            else:
                self.actor.SetHeightColorMode(False)

    def addToView(self, view):
        if view in self.views:
            return
        self.views.append(view)
        view.renderer().AddActor(self.actor)
        view.render()

    def renderAllViews(self):
        for view in self.views:
            view.render()

    def onRemoveFromObjectModel(self):
        self.removeFromAllViews()

    def removeFromAllViews(self):
        for view in list(self.views):
            self.removeFromView(view)
        assert len(self.views) == 0

    def removeFromView(self, view):
        assert view in self.views
        self.views.remove(view)
        view.renderer().RemoveActor(self.actor)
        view.render()

    def onMessage(self, msgBytes):
        print "about to draw"
        self.actor.UpdateOctomapData(msgBytes.data())
        self.renderAllViews()


managerInstance = None

class OctomapManager(object):

    def __init__(self, view):
        assert LCMGL_AVAILABLE
        self.view = view
        self.subscriber = None
        self.enable()

    def isEnabled(self):
        return self.subscriber is not None

    def setEnabled(self, enabled):
        if enabled and not self.subscriber:
            #self.subscriber = lcmUtils.addSubscriber('LCMGL.*', callback=self.onMessage)
            self.subscriber = lcmUtils.addSubscriber('OCTOMAP', callback=self.onMessage)
        elif not enabled and self.subscriber:
            lcmUtils.removeSubscriber(self.subscriber)
            self.subscriber = None

    def enable(self):
        self.setEnabled(True)

    def disable(self):
        self.setEnabled(False)

    def onMessage(self, msgBytes, channel):
        print " "
        print "got data"
        msg = lcmOctomap.raw_t.decode(msgBytes.data())
        drawObject = self.getDrawObject(channel)
        if not drawObject:
            drawObject = self.addDrawObject(channel, msgBytes)
        drawObject.onMessage(msgBytes)

    def getDrawObject(self, name):
        parent = om.getOrCreateContainer('Octomap')
        return parent.findChild(name)

    def addDrawObject(self, name, msgBytes):
        actor = vtk.vtkOctomap()
        obj = OctomapObject(name, actor)
        om.addToObjectModel(obj, om.getOrCreateContainer('Octomap'))
        obj.addToView(self.view)
        return obj


def init(view):
    if not hasattr(vtk, 'vtkOctomap'):
        return None

    global managerInstance
    managerInstance = OctomapManager(view)

    return managerInstance
