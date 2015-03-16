
# We expect that Theano works in the current Python env.

print "CRNN Python SprintInterface module load"

import os
import sys
import time
import re
from threading import Event, Thread
import thread

import numpy
import theano
import theano.tensor as T
import h5py

from SprintDataset import SprintDataset
from Log import log
from Device import get_gpu_names
import rnn
from Engine import Engine, assign_dev_data_single_seq

DefaultSprintCrnnConfig = "config/crnn.config"

startTime = None
isInitialized = False
isTrainThreadStarted = False
InputDim = None
OutputDim = None
TargetMode = None
Task = "train"

config = None; """ :type: rnn.Config """
dataset = None; """ :type: SprintDataset """
engine = None; """ :type: Engine """

lastEpochBatchModel = None; """ :type: (int,int,str|None) """  # see getLastEpochBatch()


rnn.initBetterExchook()


# Start Sprint PythonSegmentOrder interface. {

def getSegmentList(corpusName, segmentList, segmentsInfo):
  """
  Called by Sprint PythonSegmentOrder.
  Set python-segment-order = true in Sprint to use this.

  If this is used, this gets called really early.
  If it is used together with the Sprint PythonTrainer,
  it will get called way earlier before the init() below.
  It might also get called multiple times, e.g. if
  Sprint is in interactive mode to calc the seg count.

  :type corpusName: str
  :type segmentList: list[str]
  :type segmentsInfo: dict[str,dict[str]]
  :rtype: list[str]
  :returns segment list. Can also be an iterator.
  """
  print("Sprint: getSegmentList(%r)" % corpusName)
  print("Corpus segments #: %i" % len(segmentList))
  print("(This can be further filtered in Sprint by a whitelist or so.)")

  # Init what we need. These can be called multiple times.
  initBase()
  initDataset()
  with dataset.lock:
    assert not isTrainThreadStarted
    dataset.initFromSegmentOrder(segmentList, segmentsInfo)
    dataset.finalized = False

  numEpochs = getNumEpochs()
  startEpoch, startSegmentIdx = getTrainStartEpochBatch()
  print("Sprint: Starting with epoch %i, segment-idx %s." % (startEpoch, startSegmentIdx))
  print("Final epoch is: %i" % numEpochs)

  # Loop over multiple epochs. Epochs start at 1.
  for curEpoch in range(startEpoch, numEpochs + 1):
    if isTrainThreadStarted:
      waitUntilTrainerInEpoch(curEpoch)

    with dataset.lock:
      dataset.init_seq_order(epoch=curEpoch)
      segmentList = dataset.getSegmentList()

    print("Sprint epoch: %i" % curEpoch)
    startSegmentIdx = 0
    if curEpoch == startEpoch: startSegmentIdx = startSegmentIdx
    for curSegmentIdx in range(startSegmentIdx, len(segmentList)):
      yield segmentList[curSegmentIdx]

  dataset.finalize()

# End Sprint PythonSegmentOrder interface. }


# Start Sprint PythonTrainer interface. {

def init(inputDim, outputDim, config, targetMode, cudaEnabled, cudaActiveGpu):
  """
  Called by Sprint when it initializes the PythonTrainer.
  Set trainer = python-trainer in Sprint to enable.
  Note that Sprint will call this, i.e. the trainer init lazily quite late,
  only once it sees the first data.

  :type inputDim: int
  :type outputDim: int
  :param str config: config string, passed by Sprint. assumed to be ","-separated
  :param str targetMode: "target-alignment" or "criterion-by-sprint" or so
  :param bool cudaEnabled: whether Sprint has CUDA enabled
  :param int cudaActiveGpu: the GPU idx used by Sprint
  """
  print "Python train init()"
  print "inputDim:", inputDim
  print "outputDim:", outputDim
  print "config:", config
  print "targetMode:", targetMode
  print "Sprint cudaEnabled:", cudaEnabled
  print "Sprint cudaActiveGpu:", cudaActiveGpu
  global InputDim, OutputDim
  InputDim = inputDim
  OutputDim = outputDim

  config = config.split(",")
  config = {key: value for (key, value) in [s.split(":", 1) for s in config if s]}

  epoch = config.get("epoch", None)
  if epoch is not None:
    epoch = int(epoch)
    assert epoch >= 1

  global Task
  action = config["action"]
  Task = action
  if action == "train":
    pass
  elif action == "forward":
    assert targetMode == "criterion-by-sprint"  # Hack in Sprint to just pass us the features.
    targetMode = "forward"
  else:
    assert False, "unknown action: %r" % action

  initBase(targetMode=targetMode)
  if Task == "train":
    # Note: Atm, we must know all the segment info in advance.
    # The CRNN Engine.train() depends on that.
    assert dataset, "need to be inited already via segment_order mod"
    assert dataset.num_seqs > 0, "need to have data seqs"
  else:
    if not dataset:
      initDataset()
  dataset.setDimensions(inputDim, outputDim)
  dataset.initialize()

  if Task == "train":
    startTrainThread(epoch)
  elif Task == "forward":
    prepareForwarding(epoch)


def exit():
  print "Python train exit()"
  assert isInitialized
  assert isTrainThreadStarted
  trainThread.join()
  rnn.finalize()
  print >> log.v3, ("elapsed total time: %f" % (time.time() - startTime))


def feedInput(features, weights=None, segmentName=None):
  #print "feedInput", segmentName
  assert features.shape[0] == InputDim
  if Task == "train":
    posteriors = train(segmentName, features)
  elif Task == "forward":
    posteriors = forward(segmentName, features)
  else:
    assert False, "invalid task: %r" % Task
  assert posteriors.shape == (OutputDim, features.shape[1])
  return posteriors


def finishDiscard():
  print "finishDiscard()"
  raise NotImplementedError # TODO ...


def finishError(error, errorSignal, naturalPairingType=None):
  assert naturalPairingType == "softmax"
  assert Task == "train"
  # reformat. see train()
  error = numpy.array([error], dtype=theano.config.floatX)
  errorSignal = errorSignal.transpose()
  errorSignal = errorSignal[:, numpy.newaxis, :]
  errorSignal = numpy.array(errorSignal, dtype=theano.config.floatX)
  assert errorSignal.shape == Criterion.posteriors.shape

  Criterion.error = error
  Criterion.errorSignal = errorSignal
  Criterion.gotErrorSignal.set()


def feedInputAndTargetAlignment(features, targetAlignment, weights=None, segmentName=None):
  #print "feedInputAndTargetAlignment", segmentName
  assert features.shape[0] == InputDim
  train(segmentName, features, targetAlignment)


def feedInputAndTargetSegmentOrth(features, targetSegmentOrth, weights=None, segmentName=None):
  raise NotImplementedError


def feedInputUnsupervised(features, weights=None, segmentName=None):
  assert features.shape[0] == InputDim
  train(segmentName, features)

# End Sprint PythonTrainer interface. }


def dumpFlags():
  print "available GPUs:", get_gpu_names()

  import theano.sandbox.cuda as theano_cuda
  print "CUDA via", theano_cuda.__file__
  print "CUDA available:", theano_cuda.cuda_available

  print "THEANO_FLAGS:", rnn.TheanoFlags
  print "CUDA_LAUNCH_BLOCKING:", os.environ.get("CUDA_LAUNCH_BLOCKING")


def setTargetMode(mode):
  """
  :param str mode: target mode
  """
  global TargetMode
  assert config, "not initialized"
  TargetMode = mode
  task = "train"
  loss = config.value('loss', None)
  if TargetMode == "criterion-by-sprint":
    assert loss == "sprint", "TargetMode is %s but loss is %s" % (TargetMode, loss)
  elif TargetMode == "target-alignment":
    # CRNN always expects an alignment, so this is good just as-is.
    # This means that we will not calculate the criterion in Sprint.
    assert loss != "sprint", "invalid loss %s for target mode %s" % (loss, TargetMode)
  elif TargetMode == "forward":
    # Will be handled below.
    task = "forward"
    config.set("extract", ["posteriors"])
  else:
    assert False, "target-mode %s not supported yet..." % TargetMode

  if engine:
    # If we already initialized the engine, the value must not differ,
    # because e.g. Devices will init accordingly.
    orig_task = config.value("task", "train")
    assert orig_task == task

  config.set("task", task)


def initBase(configfile=None, targetMode=None):
  """
  :type configfile: str | None
  """

  global isInitialized
  isInitialized = True
  # Run through in any case. Maybe just to set targetMode.

  global config
  if not config:
    if configfile is None:
      configfile = DefaultSprintCrnnConfig
    assert os.path.exists(configfile)

    rnn.initThreadJoinHack()
    rnn.initConfig(configfile, [])
    config = rnn.config
    rnn.initLog()
    rnn.initConfigJson()

  if targetMode:
    setTargetMode(targetMode)

  global engine
  if not engine:
    modelFileName = getLastEpochBatch()[2]
    devices = rnn.initDevices()
    network = rnn.initNeuralNetwork(modelFileName)

    rnn.printTaskProperties(devices, network)
    rnn.initEngine(devices, network)
    engine = rnn.engine
    assert isinstance(engine, Engine)


def startTrainThread(epoch=None):
  global config, engine, isInitialized, isTrainThreadStarted
  assert isInitialized, "need to call init() first"
  assert not isTrainThreadStarted
  assert dataset, "need to call initDataset() first"
  assert Task == "train"

  start_epoch, start_batch = getTrainStartEpochBatch()
  # If some epoch is explicitly specified, it checks whether it matches.
  if epoch is not None:
    assert epoch == start_epoch

  def trainThreadFunc():
    try:
      assert TargetMode
      if TargetMode == "target-alignment":
        engine.train_config(config, train_data=dataset, dev_data=None, eval_data=None,
                            start_epoch=start_epoch, start_batch=start_batch)
      elif TargetMode == "criterion-by-sprint":
        # TODO ...
        raise NotImplementedError
      else:
        raise Exception("target-mode not supported: %s" % TargetMode)
    except Exception:
      try:
        print "CRNN train failed"
        sys.excepthook(*sys.exc_info())
      finally:
        # Exceptions are fatal. Stop now.
        thread.interrupt_main()

  global trainThread
  trainThread = Thread(target=trainThreadFunc, name="Sprint CRNN train thread")
  trainThread.daemon = True
  trainThread.start()

  global startTime
  startTime = time.time()
  isTrainThreadStarted = True


def prepareForwarding(epoch):
  assert engine
  assert config
  # Should already be set via setTargetMode().
  assert config.list('extract') == ["posteriors"], "You need to have extract = posteriors in your CRNN config. " + \
                                                   "You have: %s" % config.list('extract')

  lastEpoch, _, _ = getLastEpochBatch()
  assert lastEpoch == epoch  # Would otherwise require some redesign of initBase(), or reload net params here.

  # Copy over net params.
  engine.devices[0].prepare(engine.network, None)


def initDataset():
  global dataset
  if dataset: return
  dataset, _ = SprintDataset.load_data(config, rnn.getCacheSizes()[0])


def getNumEpochs():
  global config, engine
  assert engine
  assert config
  config_num_epochs = engine.config_get_num_epochs(config)
  with engine.lock:
    if engine.is_training:
      assert engine.num_epochs == config_num_epochs
  return config_num_epochs


def getLastEpochBatch():
  """
  :returns (epoch,batch,modelFilename)
  :rtype: (int, int|None, str|None)
  """
  global lastEpochBatchModel
  if lastEpochBatchModel: return lastEpochBatchModel

  global config
  assert config
  modelFileName = config.value('model', '')
  assert modelFileName, "need 'model' in config"

  from glob import glob
  files = glob(modelFileName + ".*")
  file_list = []; """ :type: list[(int,int,str)] """
  for fn in files:
    m = re.match(".*\\.([0-9]+)\\.([0-9]+)$", fn)
    if m:
      epoch, batch = map(int, m.groups())
    else:
      m = re.match(".*\\.([0-9]+)$", fn)
      if m:
        epoch = int(m.groups()[0])
        batch = None
      else:
        continue
    file_list += [(epoch, batch, fn)]
  if len(file_list) == 0:
    lastEpochBatchModel = (None, None, None)
  else:
    file_list.sort()
    lastEpochBatchModel = file_list[-1]
  return lastEpochBatchModel


def getTrainStartEpochBatch():
  """
  We will always automatically determine the best start (epoch,batch) tuple
  based on existing model files.
  This ensures that the files are present and enforces that there are
  no old outdated files which should be ignored.
  Note that epochs start at idx 1 and batches at idx 0.
  :returns (epoch,batch)
  :rtype (int,int)
  """
  last_epoch, last_batch, _ = getLastEpochBatch()
  if last_epoch is None:
    start_epoch = 1
    start_batch = 0
  elif last_batch is None:
    # No batch -> start with next epoch.
    start_epoch = last_epoch + 1
    start_batch = 0
  else:
    # Stay in last epoch, start with next batch.
    start_epoch = last_epoch
    start_batch = last_batch + 1
  return start_epoch, start_batch


def waitUntilTrainerInEpoch(epoch):
  assert isTrainThreadStarted
  assert engine
  while True:
    with engine.lock:
      if engine.training_finished: return
      if engine.is_training:
        if engine.cur_epoch == epoch: return
        assert engine.cur_epoch < epoch  # would confuse the seq order otherwise...
      engine.cond.wait()


def train(segmentName, features, targets=None):
  """
  :param str|None segmentName: full name
  :param numpy.ndarray features: 2d array
  :param numpy.ndarray|None targets: 2d or 1d array
  """
  assert engine is not None, "not initialized. call initBase()"
  assert dataset

  dataset.addNewData(segmentName, features, targets)

  # The CRNN train thread started via start() will do the actual training.

  if TargetMode == "criterion-by-sprint":

    # TODO...

    Criterion.gotPosteriors.clear()

    Criterion.gotPosteriors.wait()
    posteriors = Criterion.posteriors
    assert posteriors is not None

    # posteriors is in format (time,batch,emission)
    assert posteriors.shape[0] == T
    assert posteriors.shape[1] == 1
    assert OutputDim == posteriors.shape[2]
    assert OutputDim == engine.network.n_out
    assert len(posteriors.shape) == 3
    # reformat to Sprint expected format (emission,time)
    posteriors = posteriors[:,0,:]
    posteriors = posteriors.transpose()
    assert posteriors.shape[0] == OutputDim
    assert posteriors.shape[1] == T
    assert len(posteriors.shape) == 2

    return posteriors


def forward(segmentName, features):
  assert engine is not None, "not initialized"
  assert dataset

  # Features are in Sprint format (feature,time).
  T = features.shape[1]
  assert features.shape == (InputDim, T)

  # Init dataset with one single entry: the current segment.
  dataset.initFromSegmentOrder([segmentName], {segmentName: {"nframes": T}})
  dataset.initialize()
  dataset.epoch = -1  # Force reinit in init_seq_order().
  dataset.init_seq_order(0)  # Epoch does not matter.
  # Fill the data for the current segment.
  seq = dataset.addNewData(segmentName, features)

  # Prepare data for device.
  device = engine.devices[0]
  success = assign_dev_data_single_seq(device, dataset, seq)
  assert success, "failed to allocate & assign data for seq %i, %s" % (seq, segmentName)

  # Do the actual forwarding and collect result.
  device.run("extract")
  result = device.result()
  assert result is not None, "Device crashed."
  assert len(result) == 1
  posteriors = result[0]

  # Posteriors are in format (time,emission).
  assert posteriors.shape == (T, OutputDim)
  # Reformat to Sprint expected format (emission,time).
  posteriors = posteriors.transpose()
  assert posteriors.shape == (OutputDim, T)

  return posteriors


class Criterion(theano.Op):
  gotPosteriors = Event()
  gotErrorSignal = Event()
  posteriors = None
  error = None
  errorSignal = None

  def __eq__(self, other):
    return type(self) == type(other)

  def __hash__(self):
    return hash(type(self))

  def __str__(self):
    return self.__class__.__name__

  def make_node(self, posteriors, seq_lengths):
    # We get the posteriors here from the Network output function,
    # which should be softmax.
    posteriors = theano.tensor.as_tensor_variable(posteriors)
    seq_lengths = theano.tensor.as_tensor_variable(seq_lengths)
    assert seq_lengths.ndim == 1  # vector of seqs lengths
    return theano.Apply(op=self, inputs=[posteriors, seq_lengths], outputs=[T.fvector(), posteriors.type()])

  def perform(self, node, inputs, outputs):
    posteriors, seq_lengths = inputs
    nTimeFrames = posteriors.shape[0]
    seq_lengths = numpy.array([nTimeFrames])  # TODO: fix or so?

    self.__class__.posteriors = posteriors
    self.gotPosteriors.set()

    if numpy.isnan(posteriors).any():
      print >> log.v1, 'posteriors contain NaN!'
    if numpy.isinf(posteriors).any():
      print >> log.v1, 'posteriors contain Inf!'
      numpy.set_printoptions(threshold=numpy.nan)
      print >> log.v1, 'posteriors:', posteriors

    self.gotErrorSignal.wait()
    loss, errsig = self.error, self.errorSignal
    assert errsig.shape[0] == nTimeFrames

    outputs[0][0] = loss
    outputs[1][0] = errsig

    print >> log.v5, 'avg frame loss for segments:', loss.sum() / seq_lengths.sum(),
    print >> log.v5, 'time-frames:', seq_lengths.sum()


# HACK for now.
import SprintErrorSignals
import Network

SprintErrorSignals.SprintErrorSigOp = Criterion
Network.SprintErrorSigOp = Criterion


def demo():
  print "Note: Load this module via Sprint python-trainer to really use it."
  print "We are running a demo now."
  init(inputDim=493, outputDim=4501, config="",  # hardcoded, just a demo...
       targetMode="criterion-by-sprint", cudaEnabled=False, cudaActiveGpu=-1)
  assert os.path.exists("input-features.npy"), "run Sprint with python-trainer=dump first"
  features = numpy.load("input-features.npy")  # dumped via dump.py
  posteriors = feedInput(features)
  if not os.path.exists("posteriors.npy"):
    numpy.save("posteriors.npy", posteriors)
    print "Saved posteriors.npy. Now run Sprint with python-trainer=dump again."
    sys.exit()
  old_posteriors = numpy.load("posteriors.npy")
  assert numpy.array_equal(posteriors, old_posteriors)
  error = numpy.load("output-error.npy")  # dumped via dump.py
  error = float(error)
  errorSignal = numpy.load("output-error-signal.npy")  # dumped via dump.py
  finishError(error=error, errorSignal=errorSignal, naturalPairingType="softmax")
  exit()

if __name__ == "__main__":
  demo()
