# Use an official Python image as the base image
FROM python:3.13-slim

# Set the working directory in the container
WORKDIR /app

# Install pipenv
RUN pip install pipenv

# Copy the Pipfile and Pipfile.lock into the container
COPY Pipfile Pipfile.lock ./

# Install the dependencies using pipenv
RUN pipenv install --system --deploy

# Copy the application code into the container
COPY . .

# Expose the port (if needed, otherwise this can be omitted)
# EXPOSE 8000

# Command to run the script
CMD ["python", "dvc-availability-alerts.py", "config.yaml"]